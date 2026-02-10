from fastapi import FastAPI, HTTPException
import os
import psycopg2
from psycopg2.extras import Json

app = FastAPI()

def get_conn():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return None
    return psycopg2.connect(db_url, sslmode="require")

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/events/new_message")
def new_message(payload: dict):
    """
    Минимальный MVP:
    кладём событие в outbox_events, чтобы бот-воркер потом отправил агенту уведомление.
    payload пример:
    {
      "to_tg_user_id": 123456789,
      "title": "📩 Новое сообщение",
      "body": "Текст превью",
      "startapp": "conv_12"
    }
    """
    to_tg_user_id = payload.get("to_tg_user_id")
    if not to_tg_user_id:
        raise HTTPException(400, "to_tg_user_id required")

    conn = get_conn()
    if conn is None:
        raise HTTPException(500, "DATABASE_URL not set")

    event = {
        "to_tg_user_id": int(to_tg_user_id),
        "title": payload.get("title", "Новое событие"),
        "body": payload.get("body", ""),
        "startapp": payload.get("startapp")
    }

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO outbox_events (type, payload) VALUES (%s, %s)",
                ("notify", Json(event)),
            )

    return {"ok": True}
from fastapi import FastAPI
import os, json
import psycopg2

app = FastAPI()

def get_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/enqueue")
def enqueue(payload: dict):
    # payload пример: {"chat_id": 123456789, "text": "У вас новый отклик"}
    chat_id = int(payload["chat_id"])
    text = str(payload["text"])

    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into outbox_events(type, payload, status)
                values (%s, %s::jsonb, 'pending')
                """,
                ("tg_notify", json.dumps({"chat_id": chat_id, "text": text}))
            )
        return {"queued": True}
    finally:
        conn.close()
