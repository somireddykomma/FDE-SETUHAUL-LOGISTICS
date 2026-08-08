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
  is committed to this repo. It's synthetic demo/seed data (fake
  drivers, shipments, facilities) generated for this assignment —
  not real operational data.

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

3. The database is already at `data/setuhaul_freight_operations.db` —
   nothing to do here for local dev.
   `data/migration_001_holds_and_idempotency.sql` documents the
   holds/idempotency schema addition already applied to that file.

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

## Chat UI

Open `http://127.0.0.1:8000/` in a browser for a simple chat page
(`app/static/index.html`). Enter a driver ID, send messages, and it
restores conversation history on reload via `GET
/chat/history/{driver_id}`.

## Talking to the chatbot (API)

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

## Deploying (Render)

`render.yaml` defines a free web service so every driver can reach
the same instance from their own device.

1. In the [Render dashboard](https://dashboard.render.com/), **New +**
   → **Blueprint**, and point it at this GitHub repo. Render reads
   `render.yaml` and creates the service automatically.
2. When prompted for `OPENROUTER_API_KEY` (marked `sync: false` in
   the blueprint so it's never stored in git), paste your real key.
   `OPENROUTER_BASE_URL` and `OPENROUTER_MODEL` are already set.
3. Deploy. Render runs `pip install -r requirements.txt` then
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, and gives you
   a public URL like `https://setuhaul-driver-chat.onrender.com` —
   share that with drivers; concurrent access from multiple devices
   works out of the box (FastAPI handles concurrent requests, and
   `app/service.py` uses `BEGIN IMMEDIATE` transactions so two
   drivers racing for the same dock slot resolve safely instead of
   double-booking).

**Free-tier caveats, both expected for a demo deployment:**
- The instance spins down after ~15 minutes idle and cold-starts on
  the next request (a few seconds' delay).
- Local disk is ephemeral — a redeploy or a cold start after a long
  idle period resets `data/setuhaul_freight_operations.db` back to
  its committed seed state, discarding any bookings/holds/ETA
  updates made in between. Fine for demoing; if this needs to hold
  real, durable bookings later, move to a host with a persistent
  volume (e.g. Fly.io) or an external database.
- No authentication exists on `/chat` — anyone with a `driver_id` can
  chat as that driver. Fine for an internal demo link, not for a
  public production rollout.

## Project layout

```
app/
  main.py          FastAPI app: /chat, /chat/history, /health routes; serves app/static/
  agent.py         Conversation loop: LLM + tool calls, system prompt
  tools_schema.py  Tool (function-calling) schemas + dispatcher to app/service.py
  service.py       Transaction-safe business logic (the only DB writer)
  db.py            SQLite connection/transaction helpers
  config.py        Loads OPENROUTER_* from .env
  static/index.html  Browser chat UI
  errors.py, ids.py
data/
  setuhaul_freight_operations.db          seeded DB (synthetic demo data)
  migration_001_holds_and_idempotency.sql holds/idempotency schema addition
tests/
  test_concurrency.py   race-condition coverage for slot booking
render.yaml          Render Blueprint (see Deploying section)
```
