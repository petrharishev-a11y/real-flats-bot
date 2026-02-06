import os
import re
import time
import html
import json
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any, Tuple, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")  # without @
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID", "").strip()  # channel id like -100...

STATE_FILE = "state.json"

CHAT_TTL_SECONDS = 3600  # 1 hour inactivity auto-close

def _require_env():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if not BOT_USERNAME:
        raise RuntimeError("BOT_USERNAME is required (without @)")
    if not GROUP_CHAT_ID_RAW:
        raise RuntimeError("GROUP_CHAT_ID is required (channel id like -100...)")
    int(GROUP_CHAT_ID_RAW)

GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else 0

# =========================
# STATE (simple persistence for requests)
# =========================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"counter": 0, "requests": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("counter", 0)
        data.setdefault("requests", {})
        return data
    except Exception:
        return {"counter": 0, "requests": {}}

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def next_rid(state: Dict[str, Any]) -> str:
    state["counter"] = int(state.get("counter", 0)) + 1
    save_state(state)
    return f"R{state['counter']:03d}"

# =========================
# DATA
# =========================
@dataclass
class Request:
    rid: str
    author_id: int
    status: str
    created_at: float
    districts: str
    budget: str
    rooms: str
    term: str
    pets: str
    amenities: str
    area: str
    comment: str
    channel_message_id: Optional[int] = None

def get_req(state: Dict[str, Any], rid: str) -> Optional[Request]:
    rid = rid.strip().upper()
    raw = state["requests"].get(rid)
    if not raw:
        return None
    try:
        return Request(**raw)
    except Exception:
        return None

def put_req(state: Dict[str, Any], req: Request) -> None:
    state["requests"][req.rid] = asdict(req)
    save_state(state)

def list_my_active(state: Dict[str, Any], user_id: int) -> List[str]:
    out = []
    for rid, raw in state["requests"].items():
        if raw.get("author_id") == user_id and raw.get("status") == "active":
            out.append(rid)
    out.sort()
    return out

# =========================
# HELPERS
# =========================
def h(s: str) -> str:
    return html.escape(s or "")

def deep_link_offer(rid: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=offer_{rid}"

def agent_label(u) -> str:
    if not u:
        return "agent"
    if getattr(u, "username", None):
        return f"@{u.username}"
    return f"{u.first_name or 'Agent'} (id:{u.id})"

def is_no(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"нет", "no", "-", "0", "none", "не важно", "неважно"}

def channel_text(req: Request) -> str:
    lines = [
        f"📌 <b>Запрос {h(req.rid)}</b>",
        "",
        f"🏙 <b>Районы:</b> {h(req.districts)}",
        f"💰 <b>Бюджет:</b> {h(req.budget)}",
        f"🏠 <b>Комнаты:</b> {h(req.rooms)}",
        f"🕐 <b>Срок аренды:</b> {h(req.term)}",
        f"🐾 <b>Животные:</b> {h(req.pets)}",
        f"📐 <b>Площадь:</b> {h(req.area)}",
        f"✅ <b>Удобства:</b> {h(req.amenities)}",
    ]
    if req.comment and not is_no(req.comment):
        lines.append(f"💬 <b>Комментарий:</b> {h(req.comment)}")
    lines += ["", "👇 Нажми кнопку и отправь варианты боту (их увидит только клиент)."]
    return "\n".join(lines)

def author_created_text(req: Request) -> str:
    lines = [
        f"✅ Запрос <b>{h(req.rid)}</b> создан и опубликован в канале.",
        "Когда агент пришлёт вариант — появится кнопка «Открыть чат».",
    ]
    return "\n".join(lines)

# =========================
# CHAT SESSIONS (in-memory)
# =========================
# session_key = "R001|client_id|agent_id"
SESSIONS: Dict[str, Dict[str, Any]] = {}   # key -> {rid, client_id, agent_id, expires_at}
CURRENT: Dict[int, str] = {}              # user_id -> session_key
PENDING: Dict[int, Tuple[int, int]] = {}  # user_id -> (from_chat_id, message_id) unsent msg

def make_key(rid: str, client_id: int, agent_id: int) -> str:
    return f"{rid}|{client_id}|{agent_id}"

def parse_key(key: str) -> Tuple[str, int, int]:
    rid, c, a = key.split("|", 2)
    return rid, int(c), int(a)

def touch_session(rid: str, client_id: int, agent_id: int) -> str:
    key = make_key(rid, client_id, agent_id)
    SESSIONS[key] = {
        "rid": rid,
        "client_id": client_id,
        "agent_id": agent_id,
        "expires_at": time.time() + CHAT_TTL_SECONDS,
    }
    return key

def is_active_session(key: str) -> bool:
    s = SESSIONS.get(key)
    if not s:
        return False
    return s["expires_at"] > time.time()

def user_sessions(user_id: int) -> List[str]:
    now = time.time()
    keys = []
    for k, s in list(SESSIONS.items()):
        if s["expires_at"] <= now:
            continue
        if s["client_id"] == user_id or s["agent_id"] == user_id:
            keys.append(k)
    # newest first
    keys.sort(key=lambda k: SESSIONS[k]["expires_at"], reverse=True)
    return keys

def counterpart_id(key: str, user_id: int) -> int:
    rid, c, a = parse_key(key)
    return a if user_id == c else c

def role_prefix(key: str, sender_id: int, sender_label: str) -> str:
    rid, c, a = parse_key(key)
    if sender_id == c:
        return f"💬 <b>Клиент</b> по <b>{h(rid)}</b>:"
    return f"💬 <b>Агент {h(sender_label)}</b> по <b>{h(rid)}</b>:"

def session_buttons(key: str) -> InlineKeyboardMarkup:
    rid, c, a = parse_key(key)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Сделать активным", callback_data=f"sw|{rid}|{c}|{a}"),
        InlineKeyboardButton("🔚 Закрыть чат", callback_data=f"end|{rid}|{c}|{a}"),
    ]])

async def prompt_choose_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: bool = True) -> None:
    uid = update.effective_user.id
    keys = user_sessions(uid)
    if not keys:
        await update.effective_message.reply_text(
            "Сейчас нет активного чата.\n"
            "Если ты агент — зайди по кнопке в канале «Отправить варианты».\n"
            "Если ты клиент — открой чат через кнопку под вариантом."
        )
        return

    # store pending message to send after selection
    if pending and update.message:
        PENDING[uid] = (update.effective_chat.id, update.message.message_id)

    rows = []
    for k in keys[:8]:
        rid, c, a = parse_key(k)
        other = a if uid == c else c
        title = f"{rid} • чат с {other}"
        rows.append([InlineKeyboardButton(title, callback_data=f"sw|{rid}|{c}|{a}")])

    await update.effective_message.reply_text(
        "⚠️ Сообщение не отправлено — не выбран чат.\nВыбери куда отправить:",
        reply_markup=InlineKeyboardMarkup(rows),
    )

async def forward_any_message(context: ContextTypes.DEFAULT_TYPE, to_chat_id: int, from_chat_id: int, message_id: int) -> None:
    await context.bot.copy_message(chat_id=to_chat_id, from_chat_id=from_chat_id, message_id=message_id)

# =========================
# REQUEST CONVERSATION
# =========================
REQ_DISTRICTS, REQ_BUDGET, REQ_ROOMS, REQ_TERM, REQ_PETS, REQ_AREA, REQ_AMEN, REQ_COMMENT, REQ_CONFIRM = range(9)

async def request_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Создание запроса — только в личке с ботом.")
        return ConversationHandler.END
    context.user_data["new_req"] = {}
    await update.effective_message.reply_text("1) Какие районы? (можно несколько)")
    return REQ_DISTRICTS

async def request_districts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_req"]["districts"] = (update.message.text or "").strip()
    await update.message.reply_text("2) Бюджет? (например: $800–1200)")
    return REQ_BUDGET

async def request_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_req"]["budget"] = (update.message.text or "").strip()
    await update.message.reply_text("3) Комнаты/спальни? (например: 2к / студия)")
    return REQ_ROOMS

async def request_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_req"]["rooms"] = (update.message.text or "").strip()
    await update.message.reply_text("4) Срок аренды? (например: 6 месяцев+ / 12 месяцев+)")
    return REQ_TERM

async def request_term(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_req"]["term"] = (update.message.text or "").strip()
    await update.message.reply_text("5) Животные? (да / нет / по согласованию)")
    return REQ_PETS

async def request_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_req"]["pets"] = (update.message.text or "").strip()
    await update.message.reply_text("6) Желаемая площадь (м²)? Если не важно — «нет».")
    return REQ_AREA

async def request_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = (update.message.text or "").strip()
    context.user_data["new_req"]["area"] = t if t else "нет"
    await update.message.reply_text("7) Удобства (если обязательно): посудомойка / ванна / духовка… или «нет».")
    return REQ_AMEN

async def request_amen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = (update.message.text or "").strip()
    context.user_data["new_req"]["amenities"] = t if t else "нет"
    await update.message.reply_text("8) Комментарий (если есть). Если нет — «нет».")
    return REQ_COMMENT

async def request_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = (update.message.text or "").strip()
    context.user_data["new_req"]["comment"] = t if t else "нет"

    d = context.user_data["new_req"]
    preview = (
        "<b>Проверь:</b>\n\n"
        f"🏙 Районы: {h(d['districts'])}\n"
        f"💰 Бюджет: {h(d['budget'])}\n"
        f"🏠 Комнаты: {h(d['rooms'])}\n"
        f"🕐 Срок: {h(d['term'])}\n"
        f"🐾 Животные: {h(d['pets'])}\n"
        f"📐 Площадь: {h(d['area'])}\n"
        f"✅ Удобства: {h(d['amenities'])}\n"
        f"💬 Комментарий: {h(d['comment'])}\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data="req_publish")],
        [InlineKeyboardButton("❌ Отмена", callback_data="req_cancel")],
    ])
    await update.message.reply_text(preview, parse_mode=ParseMode.HTML, reply_markup=kb)
    return REQ_CONFIRM

async def request_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "req_cancel":
        context.user_data.pop("new_req", None)
        await q.edit_message_text("Ок, отменил.")
        return ConversationHandler.END

    if q.data != "req_publish":
        return REQ_CONFIRM

    state = context.application.bot_data["state"]
    rid = next_rid(state)

    d = context.user_data.get("new_req", {})
    req = Request(
        rid=rid,
        author_id=update.effective_user.id,
        status="active",
        created_at=time.time(),
        districts=d.get("districts", "-"),
        budget=d.get("budget", "-"),
        rooms=d.get("rooms", "-"),
        term=d.get("term", "-"),
        pets=d.get("pets", "-"),
        amenities=d.get("amenities", "нет"),
        area=d.get("area", "нет"),
        comment=d.get("comment", "нет"),
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Отправить варианты", url=deep_link_offer(rid))
    ]])

    msg = await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=channel_text(req),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        disable_notification=True,
        reply_markup=kb,
    )
    req.channel_message_id = msg.message_id

    put_req(state, req)
    context.user_data.pop("new_req", None)

    await q.edit_message_text("Опубликовал ✅")
    await context.bot.send_message(
        chat_id=req.author_id,
        text=author_created_text(req),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return ConversationHandler.END

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args:
        payload = args[0].strip()
        if payload.startswith("offer_"):
            rid = payload.replace("offer_", "", 1).strip().upper()
            await start_offer_mode(update, context, rid)
            return

    await update.effective_message.reply_text(
        "Real Flats Bot\n\n"
        "/request — создать запрос\n"
        "/my — мои запросы\n"
        "/chats — мои чаты\n"
        "/end — закрыть текущий чат\n"
        "/done — выйти из режима агента (/offer)\n"
    )

async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Команда /my — в личке с ботом.")
        return
    state = context.application.bot_data["state"]
    mine = list_my_active(state, update.effective_user.id)
    if not mine:
        await update.effective_message.reply_text("У тебя нет активных запросов.")
        return
    await update.effective_message.reply_text("Твои активные:\n" + "\n".join([f"• {r}" for r in mine]))

async def cmd_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Отправка вариантов — только в личке с ботом.")
        return
    if not context.args:
        await update.effective_message.reply_text("Напиши: /offer R001")
        return
    rid = context.args[0].strip().upper()
    await start_offer_mode(update, context, rid)

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # exits offer mode (agent)
    context.user_data.pop("offer_rid", None)
    await update.effective_message.reply_text("Ок ✅")

async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    keys = user_sessions(uid)
    if not keys:
        await update.effective_message.reply_text("Нет активных чатов.")
        return

    rows = []
    for k in keys[:10]:
        rid, c, a = parse_key(k)
        other = a if uid == c else c
        rows.append([InlineKeyboardButton(f"{rid} • чат с {other}", callback_data=f"sw|{rid}|{c}|{a}")])

    cur = CURRENT.get(uid)
    cur_txt = f"Текущий чат: {cur}" if cur else "Текущий чат: не выбран"
    await update.effective_message.reply_text(cur_txt + "\nВыбери чат:", reply_markup=InlineKeyboardMarkup(rows))

async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    key = CURRENT.get(uid)
    if not key or not is_active_session(key):
        await update.effective_message.reply_text("Нет активного чата.")
        CURRENT.pop(uid, None)
        return
    rid, c, a = parse_key(key)
    await close_session(context, rid, c, a, reason="закрыт вручную")

# =========================
# CALLBACKS (switch/open/end)
# =========================
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    # sw|R001|client|agent
    if data.startswith("sw|"):
        _, rid, c, a = data.split("|", 3)
        key = touch_session(rid, int(c), int(a))
        CURRENT[q.from_user.id] = key

        # If user had pending message -> send it now
        if q.from_user.id in PENDING:
            from_chat_id, mid = PENDING.pop(q.from_user.id)
            to_id = counterpart_id(key, q.from_user.id)
            sender_lbl = agent_label(q.from_user)
            await context.bot.send_message(
                chat_id=to_id,
                text=role_prefix(key, q.from_user.id, sender_lbl),
                parse_mode=ParseMode.HTML,
                reply_markup=session_buttons(key),
            )
            await forward_any_message(context, to_id, from_chat_id, mid)

        await q.message.reply_text("✅ Чат выбран. Просто пиши сюда.")
        return

    # open|R001|client|agent
    if data.startswith("open|"):
        _, rid, c, a = data.split("|", 3)
        key = touch_session(rid, int(c), int(a))
        # set current for who pressed
        CURRENT[q.from_user.id] = key
        await q.message.reply_text("✅ Чат открыт. Просто пиши сюда.")
        return

    # end|R001|client|agent
    if data.startswith("end|"):
        _, rid, c, a = data.split("|", 3)
        await close_session(context, rid, int(c), int(a), reason="закрыт")
        await q.message.reply_text("🔚 Чат закрыт.")
        return

# =========================
# MODES
# =========================
async def start_offer_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: str) -> None:
    state = context.application.bot_data["state"]
    req = get_req(state, rid)
    if not req or req.status != "active":
        await update.effective_message.reply_text("Этот запрос не найден или уже закрыт.")
        return
    context.user_data["offer_rid"] = rid
    await update.effective_message.reply_text(
        f"Ок. Кидай варианты по <b>{h(rid)}</b> сюда.\n"
        "Можно текст/ссылки/фото/форварды.\n"
        "Когда закончил — /done",
        parse_mode=ParseMode.HTML,
    )

async def close_session(context: ContextTypes.DEFAULT_TYPE, rid: str, client_id: int, agent_id: int, reason: str) -> None:
    key = make_key(rid, client_id, agent_id)
    SESSIONS.pop(key, None)

    # clear current if points to that key
    if CURRENT.get(client_id) == key:
        CURRENT.pop(client_id, None)
    if CURRENT.get(agent_id) == key:
        CURRENT.pop(agent_id, None)

    try:
        await context.bot.send_message(chat_id=client_id, text=f"🔚 Чат {rid} {reason}.")
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=agent_id, text=f"🔚 Чат {rid} {reason}.")
    except Exception:
        pass

# =========================
# PRIVATE ROUTER (ANY TYPE)
# =========================
async def private_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private" or not update.message:
        return

    uid = update.effective_user.id

    # 1) If user has CURRENT session -> forward to counterpart
    cur = CURRENT.get(uid)
    if cur and is_active_session(cur):
        # refresh TTL
        rid, c, a = parse_key(cur)
        touch_session(rid, c, a)
        to_id = counterpart_id(cur, uid)
        sender_lbl = agent_label(update.effective_user)

        # send context header + message
        await context.bot.send_message(
            chat_id=to_id,
            text=role_prefix(cur, uid, sender_lbl),
            parse_mode=ParseMode.HTML,
            reply_markup=session_buttons(cur),
        )
        await forward_any_message(context, to_id, update.effective_chat.id, update.message.message_id)
        return

    # 2) If user has exactly 1 active session -> auto-use it
    keys = user_sessions(uid)
    if len(keys) == 1:
        CURRENT[uid] = keys[0]
        await private_router(update, context)
        return

    # 3) Agent offer mode
    offer_rid = context.user_data.get("offer_rid")
    if offer_rid:
        state = context.application.bot_data["state"]
        req = get_req(state, offer_rid)
        if not req or req.status != "active":
            await update.effective_message.reply_text("Запрос не найден или закрыт.")
            context.user_data.pop("offer_rid", None)
            return

        agent = update.effective_user
        a_lbl = agent_label(agent)

        # create/touch session but DO NOT set current for client automatically
        key = touch_session(offer_rid, req.author_id, agent.id)

        # To client: header + OPEN CHAT button
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Открыть чат", callback_data=f"open|{offer_rid}|{req.author_id}|{agent.id}"),
        ]])

        await context.bot.send_message(
            chat_id=req.author_id,
            text=f"📩 Вариант по <b>{h(offer_rid)}</b> от <b>{h(a_lbl)}</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

        await forward_any_message(context, req.author_id, update.effective_chat.id, update.message.message_id)
        await update.effective_message.reply_text("Отправил владельцу ✅")
        return

    # 4) No current chat + many sessions -> force choose (prevents silent drops)
    if keys:
        await prompt_choose_chat(update, context, pending=True)
        return

    # 5) Nothing to do
    await update.effective_message.reply_text(
        "Я не понял, куда это отправить.\n"
        "Если ты агент — зайди по кнопке в канале.\n"
        "Если ты клиент — нажми «Открыть чат» под вариантом."
    )

# =========================
# CLEANUP JOB
# =========================
async def cleanup_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = time.time()
    expired = []
    for k, s in list(SESSIONS.items()):
        if s["expires_at"] <= now:
            expired.append(k)

    for k in expired:
        rid, c, a = parse_key(k)
        await close_session(context, rid, c, a, reason="закрыт по тайм-ауту (1 час без активности)")

def main() -> None:
    _require_env()

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["state"] = load_state()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("offer", cmd_offer))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("end", cmd_end))

    # callbacks
    app.add_handler(CallbackQueryHandler(cb_router, pattern=r"^(sw|open|end)\|"))

    # request conversation
    req_conv = ConversationHandler(
        entry_points=[CommandHandler("request", request_entry)],
        states={
            REQ_DISTRICTS: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_districts)],
            REQ_BUDGET: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_budget)],
            REQ_ROOMS: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_rooms)],
            REQ_TERM: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_term)],
            REQ_PETS: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_pets)],
            REQ_AREA: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_area)],
            REQ_AMEN: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_amen)],
            REQ_COMMENT: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_comment)],
            REQ_CONFIRM: [CallbackQueryHandler(request_confirm_cb)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(req_conv)

    # router catches ANY private message type except commands
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_router))

    # cleanup job every 2 minutes
    app.job_queue.run_repeating(cleanup_sessions, interval=120, first=120)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
