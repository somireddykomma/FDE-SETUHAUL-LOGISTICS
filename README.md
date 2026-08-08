# SetuHaul Driver Chat Service

A FastAPI service that lets truck drivers report delays, check on
appointment slots, and book warehouse dock appointments over a chat
endpoint. An LLM (via OpenRouter) drives the conversation, but every
side effect — ETA updates, slot holds, bookings — goes through a
transaction-safe service layer (`app/service.py`); the model never
touches the database directly.

## Prerequisites

- Python 3.13 (a `venv/` is already included; recreate it if you're on
  a different Python version)
- An [OpenRouter](https://openrouter.ai/) API key
- The seeded SQLite database at `data/setuhaul_freight_operations.db`
  (gitignored — it contains sample operational data, not schema-only.
  Copy it in from wherever the project data lives before running.)

## Setup

1. **Create/activate the virtualenv** (skip if `venv/` already works
   for you):

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   pip install openai python-dotenv  # used by app/agent.py, app/config.py
   ```

2. **Configure your API key**:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set:

   ```
   OPENROUTER_API_KEY=sk-or-v1-...your key...
   OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
   OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
   ```

   `OPENROUTER_MODEL` can be any tool-calling-capable model slug from
   [openrouter.ai/models](https://openrouter.ai/models) — check the
   slug is still live there before relying on it, model availability
   changes over time.

3. **Make sure the database is in place**:

   ```
   data/setuhaul_freight_operations.db
   ```

   `data/migration_001_holds_and_idempotency.sql` is included in the
   repo; apply it against the base schema if you're building the DB
   from scratch rather than copying a pre-seeded file.

## Running the service

```bash
source venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Check it's up:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

## Talking to the chatbot

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"driver_id": "DRV003", "message": "I am running late, whats going on with my shipment?"}'
```

Response:

```json
{"thread_id": "THR007", "reply": "...", "escalated": false}
```

- `driver_id` must exist in the `drivers` table (seed data uses IDs
  like `DRV001`–`DRV0xx`).
- Conversation state is keyed by `driver_id` and persisted in
  `chat_threads` / `chat_messages`, so you can keep POSTing to `/chat`
  with the same `driver_id` to continue the same thread. A thread
  closes/escalates out of the "active" set once resolved or handed to
  a human.

## Running tests

```bash
source venv/bin/activate
pytest -q
```

Tests run against a private copy of the seeded DB (see
`tests/conftest.py`) so they never mutate your working `data/*.db`.

## Project layout

```
app/
  main.py          FastAPI app, /chat and /health routes
  agent.py         Conversation loop: LLM + tool calls, system prompt
  tools_schema.py  Tool (function-calling) schemas + dispatcher to app/service.py
  service.py       Transaction-safe business logic (the only DB writer)
  db.py            SQLite connection/transaction helpers
  config.py        Loads OPENROUTER_* from .env
  errors.py, ids.py
data/
  setuhaul_freight_operations.db          seeded DB (gitignored)
  migration_001_holds_and_idempotency.sql holds/idempotency schema addition
tests/
  test_concurrency.py   race-condition coverage for slot booking
```
