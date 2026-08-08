from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.db import get_connection

app = FastAPI(title="SetuHaul Driver Chat")


class ChatRequest(BaseModel):
    driver_id: str
    message: str


class ChatResponse(BaseModel):
    thread_id: str
    reply: str
    escalated: bool


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    # Imported lazily so the API can boot (and its non-LLM routes work) even
    # if OPENROUTER_API_KEY isn't set yet -- the error surfaces per-request instead.
    from app.agent import handle_driver_message

    conn = get_connection()
    try:
        result = handle_driver_message(conn, req.driver_id, req.message)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        conn.close()
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
