"""Plain, typed service-layer functions ("tools") for the SetuHaul agent.

These are the only functions allowed to write to the operational tables.
Every write happens inside a single DB transaction (see app.db.transaction)
so the LLM layer never has to reason about partial state or locking -- it
just calls a function and gets back a result or a typed error.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.db import transaction
from app.errors import (
    AppointmentConflict,
    HoldMismatch,
    HoldNotActive,
    HoldNotFound,
    ShipmentAmbiguous,
    ShipmentNotFound,
    SlotNotFound,
    SlotUnavailable,
    ThreadNotFound,
)
from app.ids import new_id

IST = timezone(timedelta(hours=5, minutes=30))

ACTIVE_APPOINTMENT_STATUSES = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")


def now_iso() -> str:
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def _expire_stale_holds(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(
        """
        UPDATE slot_holds
        SET hold_status = 'EXPIRED'
        WHERE hold_status = 'ACTIVE' AND expires_at < ?
        """,
        (now,),
    )


# ---------------------------------------------------------------------------
# Identity / context
# ---------------------------------------------------------------------------

def get_driver_active_shipments(conn: sqlite3.Connection, driver_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT shipment_id, order_reference, destination_facility_id,
               current_status, priority_code, original_eta_ts, latest_eta_ts
        FROM shipments
        WHERE driver_id = ?
          AND current_status NOT IN ('COMPLETED', 'CANCELLED')
        ORDER BY original_eta_ts
        """,
        (driver_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_shipment_for_driver(conn: sqlite3.Connection, driver_id: str, shipment_id: str | None) -> str:
    """Disambiguation gate described in the implementation guide section 5.1.

    Never guess: if the caller didn't already know the shipment_id and the
    driver has more than one active shipment, force a clarifying question
    instead of picking one.
    """
    active = get_driver_active_shipments(conn, driver_id)
    if shipment_id:
        if any(s["shipment_id"] == shipment_id for s in active):
            return shipment_id
        raise ShipmentNotFound(f"{shipment_id} is not an active shipment for driver {driver_id}")
    if len(active) == 1:
        return active[0]["shipment_id"]
    if len(active) == 0:
        raise ShipmentNotFound(f"driver {driver_id} has no active shipments")
    raise ShipmentAmbiguous(driver_id, [s["shipment_id"] for s in active])


def get_shipment_context(conn: sqlite3.Connection, shipment_id: str) -> dict:
    row = conn.execute(
        """
        SELECT s.shipment_id, s.driver_id, s.vehicle_id, s.destination_facility_id,
               s.priority_code, s.required_dock_type, s.temperature_control_required,
               s.load_weight_kg, s.expected_unload_min, s.current_status,
               v.effective_eta_ts, v.eta_source, v.eta_confidence,
               v.appointment_id, v.slot_id, v.slot_start_ts, v.slot_end_ts,
               v.planned_dock_code, v.gate_in_ts, v.queue_state, v.queue_position,
               v.actual_dock_code
        FROM shipments s
        JOIN v_inbound_operational_state v ON v.shipment_id = s.shipment_id
        WHERE s.shipment_id = ?
        """,
        (shipment_id,),
    ).fetchone()
    if row is None:
        raise ShipmentNotFound(shipment_id)
    return dict(row)


# ---------------------------------------------------------------------------
# ETA
# ---------------------------------------------------------------------------

def record_eta_update(
    conn: sqlite3.Connection,
    shipment_id: str,
    declared_eta_ts: str,
    confidence_code: str = "MEDIUM",
    delay_reason_code: str | None = None,
    note: str | None = None,
    source_type: str = "DRIVER_DECLARED",
    reported_by_driver_id: str | None = None,
) -> dict:
    with transaction(conn) as c:
        exists = c.execute(
            "SELECT 1 FROM shipments WHERE shipment_id = ?", (shipment_id,)
        ).fetchone()
        if exists is None:
            raise ShipmentNotFound(shipment_id)

        eta_update_id = new_id("ETA")
        created_at = now_iso()
        c.execute(
            """
            INSERT INTO eta_updates
                (eta_update_id, shipment_id, source_type, reported_by_driver_id,
                 declared_eta_ts, confidence_code, delay_reason_code, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eta_update_id, shipment_id, source_type, reported_by_driver_id,
                declared_eta_ts, confidence_code, delay_reason_code, note, created_at,
            ),
        )
        c.execute(
            "UPDATE shipments SET latest_eta_ts = ?, updated_at = ? WHERE shipment_id = ?",
            (declared_eta_ts, created_at, shipment_id),
        )
        return {
            "eta_update_id": eta_update_id,
            "shipment_id": shipment_id,
            "declared_eta_ts": declared_eta_ts,
            "confidence_code": confidence_code,
        }


# ---------------------------------------------------------------------------
# Slot search
# ---------------------------------------------------------------------------

def list_feasible_slots(
    conn: sqlite3.Connection,
    shipment_id: str,
    after_ts: str | None = None,
    limit: int = 5,
) -> list[dict]:
    shipment = conn.execute(
        """
        SELECT destination_facility_id, required_dock_type, temperature_control_required,
               load_weight_kg, expected_unload_min
        FROM shipments WHERE shipment_id = ?
        """,
        (shipment_id,),
    ).fetchone()
    if shipment is None:
        raise ShipmentNotFound(shipment_id)

    if after_ts is None:
        eta_row = conn.execute(
            "SELECT effective_eta_ts FROM v_latest_eta WHERE shipment_id = ?", (shipment_id,)
        ).fetchone()
        after_ts = eta_row["effective_eta_ts"] if eta_row else now_iso()

    facility = conn.execute(
        "SELECT open_time, close_time FROM facilities WHERE facility_id = ?",
        (shipment["destination_facility_id"],),
    ).fetchone()

    last_start_row = conn.execute(
        """
        SELECT rule_value FROM facility_rules
        WHERE facility_id = ? AND rule_type = 'LAST_NEW_START_TIME' AND active_flag = 1
        """,
        (shipment["destination_facility_id"],),
    ).fetchone()
    last_start = last_start_row["rule_value"] if last_start_row else None

    now = now_iso()
    _expire_stale_holds(conn, now)

    rows = conn.execute(
        f"""
        SELECT sa.slot_id, sa.facility_id, sa.dock_code, sa.dock_type,
               sa.slot_start_ts, sa.slot_end_ts
        FROM v_slot_availability sa
        WHERE sa.facility_id = :facility_id
          AND sa.availability_status = 'AVAILABLE'
          AND sa.slot_start_ts >= :after_ts
          AND (:required_dock_type = 'ANY' OR sa.dock_type = :required_dock_type)
          AND (:temp_required = 0 OR sa.supports_refrigerated = 1)
          AND sa.max_vehicle_weight_kg >= :load_weight_kg
          AND (julianday(sa.slot_end_ts) - julianday(sa.slot_start_ts)) * 24 * 60 >= :unload_min
          AND substr(sa.slot_start_ts, 12, 5) >= :open_time
          AND substr(sa.slot_start_ts, 12, 5) < :close_time
          AND (:last_start IS NULL OR substr(sa.slot_start_ts, 12, 5) <= :last_start)
          AND NOT EXISTS (
              SELECT 1 FROM slot_holds h
              WHERE h.slot_id = sa.slot_id AND h.hold_status = 'ACTIVE' AND h.expires_at > :now
                AND h.shipment_id != :shipment_id
          )
        ORDER BY sa.slot_start_ts, sa.dock_code
        LIMIT :limit
        """,
        {
            "facility_id": shipment["destination_facility_id"],
            "after_ts": after_ts,
            "required_dock_type": shipment["required_dock_type"],
            "temp_required": shipment["temperature_control_required"],
            "load_weight_kg": shipment["load_weight_kg"],
            "unload_min": shipment["expected_unload_min"],
            "open_time": facility["open_time"],
            "close_time": facility["close_time"],
            "shipment_id": shipment_id,
            "last_start": last_start,
            "now": now,
            "limit": limit,
        },
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Holds (SHOWN -> HELD)
# ---------------------------------------------------------------------------

def hold_slot(
    conn: sqlite3.Connection,
    shipment_id: str,
    slot_id: str,
    thread_id: str,
    ttl_seconds: int = 120,
) -> dict:
    with transaction(conn) as c:
        now = now_iso()
        _expire_stale_holds(c, now)

        slot = c.execute(
            "SELECT slot_status FROM appointment_slots WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        if slot is None:
            raise SlotNotFound(slot_id)
        if slot["slot_status"] != "OPEN":
            raise SlotUnavailable(f"{slot_id} is {slot['slot_status']}")

        active_appt = c.execute(
            f"""
            SELECT 1 FROM appointments
            WHERE slot_id = ? AND appointment_status IN {ACTIVE_APPOINTMENT_STATUSES}
            """,
            (slot_id,),
        ).fetchone()
        if active_appt is not None:
            raise SlotUnavailable(f"{slot_id} already has an active appointment")

        active_hold = c.execute(
            "SELECT 1 FROM slot_holds WHERE slot_id = ? AND hold_status = 'ACTIVE'", (slot_id,)
        ).fetchone()
        if active_hold is not None:
            raise SlotUnavailable(f"{slot_id} is currently held by another thread")

        hold_id = new_id("HOLD")
        expires_at = (datetime.now(IST) + timedelta(seconds=ttl_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S+05:30"
        )
        try:
            c.execute(
                """
                INSERT INTO slot_holds
                    (hold_id, slot_id, thread_id, shipment_id, held_at, expires_at, hold_status)
                VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')
                """,
                (hold_id, slot_id, thread_id, shipment_id, now, expires_at),
            )
        except sqlite3.IntegrityError as exc:
            raise SlotUnavailable(f"{slot_id} was just held by another thread") from exc

        return {"hold_id": hold_id, "slot_id": slot_id, "expires_at": expires_at}


def cancel_hold(conn: sqlite3.Connection, hold_id: str) -> None:
    with transaction(conn) as c:
        row = c.execute(
            "SELECT hold_status FROM slot_holds WHERE hold_id = ?", (hold_id,)
        ).fetchone()
        if row is None:
            raise HoldNotFound(hold_id)
        if row["hold_status"] == "ACTIVE":
            c.execute(
                "UPDATE slot_holds SET hold_status = 'RELEASED' WHERE hold_id = ?", (hold_id,)
            )


# ---------------------------------------------------------------------------
# Appointment (REQUESTED -> CONFIRMED). The only writer of `appointments`.
# ---------------------------------------------------------------------------

def request_appointment(
    conn: sqlite3.Connection,
    shipment_id: str,
    slot_id: str,
    hold_id: str | None = None,
    booking_source: str = "DRIVER_CHAT",
) -> dict:
    with transaction(conn) as c:
        now = now_iso()
        _expire_stale_holds(c, now)

        if hold_id is not None:
            hold = c.execute(
                "SELECT * FROM slot_holds WHERE hold_id = ?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise HoldNotFound(hold_id)
            if hold["hold_status"] != "ACTIVE":
                raise HoldNotActive(f"{hold_id} is {hold['hold_status']}")
            if hold["slot_id"] != slot_id or hold["shipment_id"] != shipment_id:
                raise HoldMismatch(hold_id)

        # Supersede any existing active appointment for this shipment so the
        # unique-per-shipment index doesn't reject the new insert.
        old = c.execute(
            f"""
            SELECT appointment_id FROM appointments
            WHERE shipment_id = ? AND is_current = 1
              AND appointment_status IN {ACTIVE_APPOINTMENT_STATUSES}
            """,
            (shipment_id,),
        ).fetchone()
        if old is not None:
            c.execute(
                """
                UPDATE appointments
                SET appointment_status = 'CANCELLED', is_current = 0,
                    cancelled_at = ?, cancellation_reason = 'RESCHEDULED', updated_at = ?
                WHERE appointment_id = ?
                """,
                (now, now, old["appointment_id"]),
            )

        appointment_id = new_id("APT")
        try:
            c.execute(
                """
                INSERT INTO appointments
                    (appointment_id, shipment_id, slot_id, appointment_status,
                     booking_source, is_current, booked_at, replaced_appointment_id, updated_at)
                VALUES (?, ?, ?, 'PENDING_CONFIRMATION', ?, 1, ?, ?, ?)
                """,
                (
                    appointment_id, shipment_id, slot_id, booking_source, now,
                    old["appointment_id"] if old else None, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # ux_active_appointment_per_slot fired: someone else's transaction
            # committed a PENDING_CONFIRMATION/CONFIRMED/IN_PROGRESS row first.
            raise AppointmentConflict(f"slot {slot_id} was just taken") from exc

        if hold_id is not None:
            c.execute(
                "UPDATE slot_holds SET hold_status = 'CONVERTED' WHERE hold_id = ?", (hold_id,)
            )

        return {
            "appointment_id": appointment_id,
            "shipment_id": shipment_id,
            "slot_id": slot_id,
            "appointment_status": "PENDING_CONFIRMATION",
        }


def get_appointment_status(conn: sqlite3.Connection, shipment_id: str) -> dict | None:
    row = conn.execute(
        f"""
        SELECT a.appointment_id, a.appointment_status, a.booked_at, a.confirmed_at,
               sl.slot_start_ts, sl.slot_end_ts, d.dock_code
        FROM appointments a
        JOIN appointment_slots sl ON sl.slot_id = a.slot_id
        JOIN docks d ON d.dock_id = sl.dock_id
        WHERE a.shipment_id = ? AND a.is_current = 1
          AND a.appointment_status IN {ACTIVE_APPOINTMENT_STATUSES}
        """,
        (shipment_id,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def escalate(conn: sqlite3.Connection, thread_id: str, reason: str) -> None:
    with transaction(conn) as c:
        row = c.execute(
            "SELECT thread_id FROM chat_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise ThreadNotFound(thread_id)
        now = now_iso()
        c.execute(
            "UPDATE chat_threads SET thread_status = 'ESCALATED' WHERE thread_id = ?",
            (thread_id,),
        )
        c.execute(
            """
            INSERT INTO chat_messages
                (chat_message_id, thread_id, sender_type, message_text, message_ts, requires_human_review)
            VALUES (?, ?, 'SYSTEM', ?, ?, 1)
            """,
            (new_id("MSG"), thread_id, f"Escalated to operations: {reason}", now),
        )


# ---------------------------------------------------------------------------
# Idempotency wrapper (used by the API/agent layer around mutating tools)
# ---------------------------------------------------------------------------

def idempotent_call(conn: sqlite3.Connection, idempotency_key: str, tool_name: str, request_payload: dict, func):
    existing = conn.execute(
        "SELECT response_json FROM idempotency_keys WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return json.loads(existing["response_json"])

    result = func()
    conn.execute(
        """
        INSERT INTO idempotency_keys (idempotency_key, tool_name, request_hash, response_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (idempotency_key, tool_name, json.dumps(request_payload, sort_keys=True), json.dumps(result), now_iso()),
    )
    return result
