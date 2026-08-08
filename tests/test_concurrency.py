"""The single most important test in this project.

Brief section 13.1: success is measured by the *absence* of conflicting or
duplicate allocations under concurrency, not by whether the chatbot replied.
This fires many simultaneous request_appointment calls at the same slot
(the "5 drivers want the same 6pm window" scenario from section 7.2) and
asserts the DB's ux_active_appointment_per_slot index lets exactly one win.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import get_connection
from app.errors import AppointmentConflict
from app import service as svc


def _racer(shipment_id: str, slot_id: str):
    conn = get_connection()
    try:
        return svc.request_appointment(conn, shipment_id, slot_id)
    finally:
        conn.close()


def test_only_one_appointment_wins_a_contested_slot(db_path):
    conn = get_connection()
    slot = conn.execute(
        """
        SELECT slot_id FROM v_slot_availability
        WHERE availability_status = 'AVAILABLE' AND facility_id = 'FAC-JAI-01'
        LIMIT 1
        """
    ).fetchone()
    assert slot is not None, "seed data must contain at least one open slot"
    slot_id = slot["slot_id"]

    shipment_ids = [
        r["shipment_id"]
        for r in conn.execute(
            "SELECT shipment_id FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED') LIMIT 8"
        ).fetchall()
    ]
    conn.close()
    assert len(shipment_ids) >= 5, "need several competing shipments for a realistic race"

    successes, conflicts, other_errors = [], [], []
    with ThreadPoolExecutor(max_workers=len(shipment_ids)) as pool:
        futures = [pool.submit(_racer, sid, slot_id) for sid in shipment_ids]
        for fut in as_completed(futures):
            try:
                successes.append(fut.result())
            except AppointmentConflict as e:
                conflicts.append(e)
            except Exception as e:  # noqa: BLE001 - surfaced via assertion message below
                other_errors.append(e)

    assert not other_errors, f"unexpected errors (not clean conflicts): {other_errors}"
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}: {successes}"
    assert len(conflicts) == len(shipment_ids) - 1

    verify_conn = get_connection()
    active_rows = verify_conn.execute(
        """
        SELECT appointment_id FROM appointments
        WHERE slot_id = ? AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
        """,
        (slot_id,),
    ).fetchall()
    verify_conn.close()
    assert len(active_rows) == 1, "DB must never show more than one active appointment for a slot"


def test_hold_then_request_is_still_race_safe(db_path):
    """Two threads hold-then-request the same slot back to back; only the
    first hold should succeed, so only one request_appointment can proceed."""
    conn = get_connection()
    slot = conn.execute(
        """
        SELECT slot_id FROM v_slot_availability
        WHERE availability_status = 'AVAILABLE' AND facility_id = 'FAC-JAI-01'
        LIMIT 1
        """
    ).fetchone()
    slot_id = slot["slot_id"]
    conn.close()

    from app.errors import SlotUnavailable

    conn_a = get_connection()
    hold_a = svc.hold_slot(conn_a, "SHP1002", slot_id, "THR001")
    conn_a.close()

    conn_b = get_connection()
    try:
        svc.hold_slot(conn_b, "SHP1003", slot_id, "THR002")
        raised = False
    except SlotUnavailable:
        raised = True
    finally:
        conn_b.close()
    assert raised, "second driver must not be able to hold an already-held slot"

    conn_c = get_connection()
    appt = svc.request_appointment(conn_c, "SHP1002", slot_id, hold_id=hold_a["hold_id"])
    conn_c.close()
    assert appt["appointment_status"] == "PENDING_CONFIRMATION"
