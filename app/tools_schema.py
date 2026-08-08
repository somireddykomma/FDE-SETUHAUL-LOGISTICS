"""OpenAI-compatible tool (function-calling) schemas for the SetuHaul agent,
plus a dispatcher that maps a tool call onto app.service functions.

Identity fields (driver_id, thread_id) are never LLM-settable arguments --
they come from the authenticated session context established before the
agent loop starts (see app/agent.py). A driver's free-text message cannot
claim to be a different driver_id and have that trusted; only shipment_id /
slot_id / hold_id, which the model discovers via tool results, are passed
back in by the model.
"""
from __future__ import annotations

import sqlite3

from app import service as svc
from app.errors import ServiceError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_active_shipments",
            "description": (
                "Get the current driver's profile (name) plus their active (non-completed, "
                "non-cancelled) shipments, each with destination facility name/city, vehicle "
                "registration number, and latest ETA. Call this first in every new thread -- before "
                "asking or answering anything else -- so you can open with a real, grounded summary "
                "of who the driver is and what they're hauling, and whenever it's unclear which "
                "shipment a later message refers to."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shipment_context",
            "description": (
                "Get the full operational picture for one shipment: destination facility, "
                "current appointment (if any), latest declared ETA, and gate/yard/dock status."
            ),
            "parameters": {
                "type": "object",
                "properties": {"shipment_id": {"type": "string"}},
                "required": ["shipment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_eta_update",
            "description": (
                "Record a new arrival-time estimate declared by the driver. Use this whenever the "
                "driver reports a delay or corrects a previous estimate. declared_eta_ts must be a "
                "full ISO-8601 timestamp with +05:30 offset, e.g. 2026-08-04T19:10:00+05:30 -- "
                "compute it from the shipment's current context plus the delay the driver described; "
                "do not ask the driver to produce ISO timestamps themselves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shipment_id": {"type": "string"},
                    "declared_eta_ts": {"type": "string", "description": "ISO-8601 timestamp, +05:30 offset"},
                    "confidence_code": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "delay_reason_code": {
                        "type": "string",
                        "enum": ["TRAFFIC", "BREAKDOWN", "WEATHER", "LATE_DEPARTURE", "LOADING_DELAY", "ROUTE_ISSUE", "OTHER"],
                    },
                    "note": {"type": "string", "description": "short free-text context, e.g. the driver's own words"},
                },
                "required": ["shipment_id", "declared_eta_ts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_feasible_slots",
            "description": (
                "List real, currently-available appointment slots for a shipment's destination facility, "
                "already filtered for dock compatibility, weight, refrigeration, operating hours and the "
                "shipment's latest ETA. Never invent slot times yourself -- always call this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shipment_id": {"type": "string"},
                    "after_ts": {
                        "type": "string",
                        "description": "Only show slots starting at/after this ISO-8601 timestamp. Omit to use the shipment's latest effective ETA.",
                    },
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["shipment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hold_slot",
            "description": (
                "Softly and temporarily reserve a slot (about 2 minutes) while the driver decides. "
                "This does NOT book the appointment. Use this when a driver shows interest in a specific "
                "option but hasn't explicitly confirmed yet, so it isn't given away mid-conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shipment_id": {"type": "string"},
                    "slot_id": {"type": "string"},
                },
                "required": ["shipment_id", "slot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_hold",
            "description": "Release a soft hold, e.g. the driver changed their mind about an option.",
            "parameters": {
                "type": "object",
                "properties": {"hold_id": {"type": "string"}},
                "required": ["hold_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_appointment",
            "description": (
                "Commit to booking a slot for a shipment. This is the only action that creates a real "
                "appointment. Only call this after the driver has explicitly confirmed a specific slot "
                "(e.g. 'book it', 'take the second one') -- never on an ambiguous or implied choice. "
                "If a hold_id exists for this slot/shipment pass it along. This can fail if another "
                "driver just took the slot -- if it does, apologise briefly and re-run list_feasible_slots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "shipment_id": {"type": "string"},
                    "slot_id": {"type": "string"},
                    "hold_id": {"type": "string"},
                },
                "required": ["shipment_id", "slot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_appointment_status",
            "description": "Check the current booking status for a shipment, e.g. when a driver asks 'has it been confirmed?'.",
            "parameters": {
                "type": "object",
                "properties": {"shipment_id": {"type": "string"}},
                "required": ["shipment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": (
                "Hand this conversation to a human operations coordinator. Use this when there is no "
                "feasible slot, the driver reports a safety issue, stored data contradicts what the driver "
                "says, or two consecutive tool calls have failed. Do not try to invent an answer instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


def call_tool(conn: sqlite3.Connection, driver_id: str, thread_id: str, name: str, arguments: dict) -> dict:
    """Execute one tool call. Always returns a JSON-serialisable dict, never
    raises -- errors are surfaced as {"error": ...} so the model can react
    (re-list slots, ask a clarifying question, escalate) instead of the
    whole turn crashing.
    """
    try:
        if name == "list_active_shipments":
            return {
                "driver": svc.get_driver_profile(conn, driver_id),
                "shipments": svc.get_driver_active_shipments(conn, driver_id),
            }

        if name == "get_shipment_context":
            return svc.get_shipment_context(conn, arguments["shipment_id"])

        if name == "record_eta_update":
            return svc.record_eta_update(
                conn,
                shipment_id=arguments["shipment_id"],
                declared_eta_ts=arguments["declared_eta_ts"],
                confidence_code=arguments.get("confidence_code", "MEDIUM"),
                delay_reason_code=arguments.get("delay_reason_code"),
                note=arguments.get("note"),
                reported_by_driver_id=driver_id,
            )

        if name == "list_feasible_slots":
            slots = svc.list_feasible_slots(
                conn,
                shipment_id=arguments["shipment_id"],
                after_ts=arguments.get("after_ts"),
                limit=arguments.get("limit", 5),
            )
            return {"slots": slots}

        if name == "hold_slot":
            return svc.hold_slot(
                conn,
                shipment_id=arguments["shipment_id"],
                slot_id=arguments["slot_id"],
                thread_id=thread_id,
            )

        if name == "cancel_hold":
            svc.cancel_hold(conn, arguments["hold_id"])
            return {"released": True}

        if name == "request_appointment":
            return svc.request_appointment(
                conn,
                shipment_id=arguments["shipment_id"],
                slot_id=arguments["slot_id"],
                hold_id=arguments.get("hold_id"),
            )

        if name == "get_appointment_status":
            status = svc.get_appointment_status(conn, arguments["shipment_id"])
            return status or {"appointment": None}

        if name == "escalate":
            svc.escalate(conn, thread_id, arguments["reason"])
            return {"escalated": True}

        return {"error": "UnknownTool", "message": f"no such tool: {name}"}

    except ServiceError as e:
        return {"error": type(e).__name__, "message": str(e)}
