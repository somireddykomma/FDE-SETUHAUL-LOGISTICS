-- Additive migration on top of setuhaul_schema_and_seed.sql
-- Adds a soft, short-lived hold on a slot (SHOWN -> HELD) and an idempotency
-- ledger for tool calls, per implementation_guide.md section 4.

CREATE TABLE IF NOT EXISTS slot_holds (
    hold_id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id),
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    held_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    hold_status TEXT NOT NULL
        CHECK (hold_status IN ('ACTIVE','EXPIRED','CONVERTED','RELEASED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_active_hold_per_slot
ON slot_holds(slot_id) WHERE hold_status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS ix_slot_holds_thread
ON slot_holds(thread_id, hold_status);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL
);
