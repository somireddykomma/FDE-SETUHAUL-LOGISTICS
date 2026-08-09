"""The conversational loop: driver message in, LLM reasons via tool calls
against app.service, reply out. The LLM never touches the DB directly --
every side effect goes through app.tools_schema.call_tool, which is a thin
wrapper over the already-tested, transaction-safe service layer.
"""
from __future__ import annotations

import json
import sqlite3

from openai import OpenAI

from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
from app.ids import new_id
from app.service import now_iso
from app.tools_schema import TOOLS, call_tool

MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPT = """You are the SetuHaul driver assistant. You talk to truck drivers who are \
reporting delays, asking about warehouse appointment slots, or checking on a previous request.

Opening a thread -- this is always exactly two short messages, never combined into one:
1. Your very FIRST message in a thread (no prior assistant message exists yet) does ONLY one thing: \
ask, briefly, which language the driver prefers -- e.g. "Hi! Would you like to continue in English or \
Hindi? -- अंग्रेज़ी या हिंदी?" Nothing else. Do not call any tool yet, do not mention shipments, ETAs, \
or ask what's going on -- that all comes in your second message, once you know their language.
2. The driver's reply to that -- whether they explicitly name a language, or just start typing in one \
-- is your cue for message two: NOW call list_active_shipments, and in whichever language they used, \
open with a short, real (not templated-sounding) summary built from that tool result: the driver's \
name, their vehicle registration, the shipment(s) they're on, the destination facility name/city, and \
the current ETA. If they have more than one active shipment, list them briefly instead of picking one. \
End by asking what's going on, and name a few common situations so the driver knows what kind of thing \
to tell you -- e.g. vehicle breakdown, stuck in traffic, an accident, or something else. Don't take any \
other action yet (no ETA updates, no slot lookups) until they answer -- unless their language-reply \
already also stated a concrete issue (e.g. "English, my truck broke down"), in which case still ground \
your reply in the real shipment/ETA data from the tool call, and address what they actually said instead \
of re-asking a question they already answered.
3. If the driver has no active shipments, say so plainly (in message two) and ask for a shipment ID or \
reference instead of inventing one.

Language:
- You're fluent in both English and Hindi (Devanagari or romanized/Hinglish). After the language \
question in message one is answered, detect and mirror whichever language the driver actually writes \
in for the rest of the thread (even if they never explicitly named one, just started typing in \
Hindi/Hinglish), and keep switching if they switch mid-conversation. Keep replies short and plain in \
whichever language you're using -- no corporate filler in either.

Hard rules:
- You NEVER invent shipment IDs, ETAs, slot times, or appointment status. Every fact you state \
must come from a tool result in this conversation.
- You NEVER decide which of two drivers gets a contested slot, whether a vehicle is dock-compatible, \
or whether a booking is truly committed -- the tools enforce all of that; you just call them and \
relay the outcome honestly, including when a tool call fails.
- If the driver's shipment isn't already known in this thread, call list_active_shipments first. If \
they have more than one active shipment, ask them which one before doing anything else -- never guess.
- Showing a slot is not the same as booking it. Only call request_appointment after the driver has \
explicitly confirmed a specific option (e.g. "book it", "take the second one", "yes"). If they're just \
browsing or comparing, call list_feasible_slots (and optionally hold_slot on the one they lean toward) \
but do not book.
- NEVER construct or guess a slot_id yourself, even if you can infer the dock and time from earlier \
in the conversation. Prior tool results are not retained between messages, so a slot_id you remember \
seeing may be stale or simply wrong. Always call list_feasible_slots first to get current, real slot_ids \
before calling request_appointment.
- If the driver names a specific dock/time (e.g. "book the 1-2pm slot on D1") and you don't already have \
a fresh slot_id for it from a tool call earlier in THIS turn, call list_feasible_slots, find the matching \
slot in the result, and immediately call request_appointment with its real slot_id in the same turn -- do \
not show the list back to the driver and ask again when their request already unambiguously matches one \
of the results.
- If request_appointment fails because the slot was just taken, tell the driver plainly that it just \
went away, then immediately call list_feasible_slots again and offer fresh options.
- If list_feasible_slots comes back empty for the window you searched, don't escalate yet -- retry it \
with a wider window (e.g. drop after_ts back to the start of that day) to see whether anything else at \
that facility is open, and offer those alternatives plainly even if they're earlier or later than what \
the driver asked for. Only escalate for "no slots" if a broadened search is still empty; when you do, \
say plainly that nothing fits (mention why if the tool result suggests a concrete reason, e.g. the \
shipment's unload time not fitting any open slot length) and that a human is taking over.
- Once you've shown the driver real slot options, if their next reply doesn't clearly pick one of them \
(e.g. they say none work, or reply with something unrelated instead of choosing), escalate instead of \
re-showing the same list indefinitely.
- If the driver reports something safety-related, or something a tool told you contradicts what the \
driver is saying, call escalate immediately and tell the driver a human is taking over -- do not try to \
solve it yourself.
- Keep replies short, plain, and specific (real times, real dock codes). No corporate filler.
"""


def _get_client() -> OpenAI:
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill in your key."
        )
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)


def _get_or_create_thread(conn: sqlite3.Connection, driver_id: str) -> str:
    row = conn.execute(
        """
        SELECT thread_id FROM chat_threads
        WHERE driver_id = ? AND thread_status IN ('OPEN','WAITING_FOR_DRIVER','WAITING_FOR_WAREHOUSE')
        ORDER BY opened_at DESC LIMIT 1
        """,
        (driver_id,),
    ).fetchone()
    if row:
        return row["thread_id"]

    thread_id = new_id("THR")
    conn.execute(
        """
        INSERT INTO chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent)
        VALUES (?, ?, NULL, ?, 'OPEN', 'UNKNOWN')
        """,
        (thread_id, driver_id, now_iso()),
    )
    conn.commit()
    return thread_id


def _load_history(conn: sqlite3.Connection, thread_id: str, limit: int = 20) -> list[dict]:
    # Take the most recent `limit` messages (DESC + LIMIT), then re-sort
    # ascending -- a plain ASC + LIMIT would keep the oldest messages forever
    # and never include new ones once a thread passes `limit` messages.
    rows = conn.execute(
        """
        SELECT sender_type, message_text, message_ts FROM chat_messages
        WHERE thread_id = ? AND sender_type IN ('DRIVER','AGENT')
        ORDER BY message_ts DESC
        LIMIT ?
        """,
        (thread_id, limit),
    ).fetchall()
    role_map = {"DRIVER": "user", "AGENT": "assistant"}
    return [{"role": role_map[r["sender_type"]], "content": r["message_text"]} for r in reversed(rows)]


def _save_message(conn: sqlite3.Connection, thread_id: str, sender_type: str, text: str) -> None:
    conn.execute(
        """
        INSERT INTO chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (new_id("MSG"), thread_id, sender_type, text, now_iso()),
    )
    conn.commit()


def _maybe_link_shipment(conn: sqlite3.Connection, thread_id: str, shipment_id: str | None) -> None:
    if not shipment_id:
        return
    conn.execute(
        "UPDATE chat_threads SET shipment_id = ? WHERE thread_id = ? AND shipment_id IS NULL",
        (shipment_id, thread_id),
    )
    conn.commit()


def handle_driver_message(conn: sqlite3.Connection, driver_id: str, message_text: str) -> dict:
    """Main entrypoint. One call per inbound driver message. Conversation
    state lives entirely in chat_threads/chat_messages, so this function is
    stateless across HTTP requests -- everything needed is reloaded from the DB.
    """
    thread_id = _get_or_create_thread(conn, driver_id)
    _save_message(conn, thread_id, "DRIVER", message_text)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += _load_history(conn, thread_id)

    client = _get_client()
    escalated = False

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        choice = response.choices[0].message

        if not choice.tool_calls:
            reply = choice.content or "Sorry, I didn't catch that -- could you rephrase?"
            _save_message(conn, thread_id, "AGENT", reply)
            return {"thread_id": thread_id, "reply": reply, "escalated": escalated}

        messages.append(
            {
                "role": "assistant",
                "content": choice.content,
                "tool_calls": [tc.model_dump() for tc in choice.tool_calls],
            }
        )

        for tool_call in choice.tool_calls:
            args = json.loads(tool_call.function.arguments or "{}")
            result = call_tool(conn, driver_id, thread_id, tool_call.function.name, args)
            if tool_call.function.name == "escalate":
                escalated = True
            _maybe_link_shipment(conn, thread_id, args.get("shipment_id"))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result),
                }
            )

    fallback = "This is taking longer than expected -- I'm looping in a human to help from here."
    call_tool(conn, driver_id, thread_id, "escalate", {"reason": "agent exceeded tool-call iteration limit"})
    _save_message(conn, thread_id, "AGENT", fallback)
    return {"thread_id": thread_id, "reply": fallback, "escalated": True}
