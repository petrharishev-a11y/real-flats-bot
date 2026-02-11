import os
from fastapi import FastAPI, Header, HTTPException
import psycopg2
from psycopg2.extras import Json

app = FastAPI()

DB = os.environ["DATABASE_URL"]
API_KEY = os.environ.get("API_KEY")  # можно не ставить, но лучше

def get_conn():
    # если DB из Render internal URL — ssl обычно не нужен, но можно оставить require
    return psycopg2.connect(DB)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/events/new_message")
def new_message(payload: dict, x_api_key: str | None = Header(default=None)):
    """
    payload пример:
    {
      "to_tg_user_id": 123456789,
      "title": "📩 Новое сообщение",
      "body": "Текст превью",
      "startapp": "conv_12"
    }
    """

    # защита (если включишь API_KEY в Render env)
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")

    to_tg_user_id = payload.get("to_tg_user_id")
    if not to_tg_user_id:
        raise HTTPException(status_code=400, detail="to_tg_user_id required")

    title = payload.get("title", "Новое событие")
    body = payload.get("body", "")
    startapp = payload.get("startapp")

    # текст, который реально уйдёт в Telegram
    text = f"{title}\n{body}".strip()
    if startapp:
        text += f"\n\nОткрыть: {startapp}"

    event = {"chat_id": int(to_tg_user_id), "text": text}

    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outbox_events (type, payload, status) VALUES (%s, %s, 'pending')",
                ("tg_notify", Json(event)),
            )
        return {"ok": True}
    finally:
        conn.close()
