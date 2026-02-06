import os
import re
import time
import html
import json
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any, Tuple

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

STATE_FILE = "state.json"  # simple persistence

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
# STATE (simple persistence)
# =========================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"counter": 0, "requests": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "counter" not in data:
            data["counter"] = 0
        if "requests" not in data:
            data["requests"] = {}
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
    return f"R{state['counter']:03d}"  # R001, R002 ...

# =========================
# DATA
# =========================
@dataclass
class Request:
    rid: str
    author_id: int
    status: str  # active/closed
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

def list_my_active(state: Dict[str, Any], user_id: int):
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
    # button in CHANNEL
    return f"https://t.me/{BOT_USERNAME}?start=offer_{rid}"

def parse_rid(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"\b(R\d{3,6})\b", text.upper())
    return m.group(1) if m else None

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
        "",
        f"🏙 Районы: {h(req.districts)}",
        f"💰 Бюджет: {h(req.budget)}",
        f"🏠 Комнаты: {h(req.rooms)}",
        f"🕐 Срок: {h(req.term)}",
        f"🐾 Животные: {h(req.pets)}",
        f"📐 Площадь: {h(req.area)}",
        f"✅ Удобства: {h(req.amenities)}",
    ]
    if req.comment and not is_no(req.comment):
        lines.append(f"💬 Комментарий: {h(req.comment)}")
    return "\n".join(lines)

def agent_label(u) -> str:
    if not u:
        return "agent"
    if getattr(u, "username", None):
        return f"@{u.username}"
    return f"{u.first_name or 'Agent'} (id:{u.id})"

# =========================
# Reply context (so client can Reply without pressing button)
# key: (client_id, header_message_id) -> (rid, agent_id)
# =========================
REPLY_CTX: Dict[Tuple[int, int], Tuple[str, int]] = {}

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

    # post to channel with URL button (offer)
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
        "Привет! Я бот Real Flats.\n\n"
        "Создать запрос: /request\n"
        "Мои активные: /my\n"
        "Агентам: /offer R001\n"
        "Выход из режима: /done\n"
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

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Канал = витрина запросов.\n"
        "Агенты отправляют варианты только боту — другие агенты не видят.\n\n"
        "Клиент: /request\n"
        "Агент: /offer R001 (или кнопка в канале)\n"
        "Выход: /done\n"
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("mode", None)
    context.user_data.pop("offer_rid", None)
    context.user_data.pop("reply_rid", None)
    context.user_data.pop("reply_agent_id", None)
    context.user_data.pop("new_req", None)
    await update.effective_message.reply_text("Ок.")
    return ConversationHandler.END

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("mode"):
        context.user_data.pop("mode", None)
        context.user_data.pop("offer_rid", None)
        context.user_data.pop("reply_rid", None)
        context.user_data.pop("reply_agent_id", None)
        await update.effective_message.reply_text("Готово ✅")
    else:
        await update.effective_message.reply_text("Нечего завершать.")

async def cmd_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Отправка вариантов — только в личке с ботом.")
        return
    if not context.args:
        await update.effective_message.reply_text("Напиши: /offer R001")
        return
    rid = context.args[0].strip().upper()
    await start_offer_mode(update, context, rid)

# =========================
# MODES
# =========================
# user_data["mode"] = "offer" or "reply"
async def start_offer_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: str) -> None:
    state = context.application.bot_data["state"]
    req = get_req(state, rid)
    if not req or req.status != "active":
        await update.effective_message.reply_text("Этот запрос не найден или уже закрыт.")
        return
    context.user_data["mode"] = "offer"
    context.user_data["offer_rid"] = rid
    await update.effective_message.reply_text(
        f"Ок. Кидай варианты по <b>{h(rid)}</b> сюда.\n"
        "Можно текст/ссылки/фото/форварды.\n"
        "Когда закончил — /done",
        parse_mode=ParseMode.HTML,
    )

async def start_reply_mode(context: ContextTypes.DEFAULT_TYPE, chat_id: int, rid: str, agent_id: int) -> None:
    # set reply mode for this user
    context.user_data["mode"] = "reply"
    context.user_data["reply_rid"] = rid
    context.user_data["reply_agent_id"] = agent_id
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✉️ Ок. Напиши сообщение агенту по <b>{h(rid)}</b> (можно медиа).\nВыход: /done",
        parse_mode=ParseMode.HTML,
    )

# =========================
# CALLBACKS (reply button in client chat)
# =========================
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if data.startswith("reply|"):
        # reply|R001|8132292568
        parts = data.split("|")
        if len(parts) != 3:
            return
        rid = parts[1].strip().upper()
        agent_id = int(parts[2])
        await start_reply_mode(context, q.message.chat_id, rid, agent_id)
        return

# =========================
# PRIVATE ROUTER (ANY TYPE)
# =========================
async def private_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private" or not update.message:
        return

    mode = context.user_data.get("mode")

    # If user replied (Telegram Reply) to our offer-header message -> infer reply ctx
    if not mode and update.message.reply_to_message:
        key = (update.effective_user.id, update.message.reply_to_message.message_id)
        if key in REPLY_CTX:
            rid, agent_id = REPLY_CTX[key]
            # one-shot: set reply mode and process immediately
            context.user_data["mode"] = "reply"
            context.user_data["reply_rid"] = rid
            context.user_data["reply_agent_id"] = agent_id
            mode = "reply"

    if mode == "offer":
        rid = context.user_data.get("offer_rid")
        if not rid:
            await update.effective_message.reply_text("Сначала укажи запрос: /offer R001")
            return

        state = context.application.bot_data["state"]
        req = get_req(state, rid)
        if not req or req.status != "active":
            await update.effective_message.reply_text("Запрос не найден или закрыт.")
            return

        agent = update.effective_user
        a_label = agent_label(agent)

        # Send header to client with CALLBACK button (reliable)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✉️ Ответить агенту", callback_data=f"reply|{rid}|{agent.id}")
        ]])

        header_msg = await context.bot.send_message(
            chat_id=req.author_id,
            text=f"📩 Вариант по <b>{h(rid)}</b> от <b>{h(a_label)}</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

        # store context so client can just "Reply" to this header
        REPLY_CTX[(req.author_id, header_msg.message_id)] = (rid, agent.id)

        # Copy ANY message type to client
        try:
            await context.bot.copy_message(
                chat_id=req.author_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            await update.effective_message.reply_text("Отправил владельцу ✅")
        except Exception:
            await update.effective_message.reply_text("Не смог отправить владельцу (возможно он не нажал /start).")
        return

    if mode == "reply":
        rid = context.user_data.get("reply_rid")
        agent_id = context.user_data.get("reply_agent_id")

        if not rid or not agent_id:
            await update.effective_message.reply_text("Контекст ответа потерян. Нажми кнопку «Ответить агенту» ещё раз.")
            return

        sender = update.effective_user
        s_label = agent_label(sender)

        # Header to agent
        await context.bot.send_message(
            chat_id=agent_id,
            text=f"💬 Сообщение по <b>{h(rid)}</b> от <b>{h(s_label)}</b>:",
            parse_mode=ParseMode.HTML,
        )

        # Copy ANY message type to agent
        try:
            await context.bot.copy_message(
                chat_id=agent_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            await update.effective_message.reply_text("Отправил агенту ✅")
        except Exception:
            await update.effective_message.reply_text("Не смог отправить агенту. Возможно агент ещё не нажал /start у бота.")

        # auto-exit reply mode after 1 message (safer)
        context.user_data.pop("mode", None)
        context.user_data.pop("reply_rid", None)
        context.user_data.pop("reply_agent_id", None)
        return

    # no mode -> ignore
    return

# =========================
# MAIN
# =========================
def main() -> None:
    _require_env()

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["state"] = load_state()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("offer", cmd_offer))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # callbacks (reply button)
    app.add_handler(CallbackQueryHandler(cb_router, pattern=r"^reply\|"))

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
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(req_conv)

    # router catches ANY private message type (photo/video/forward/text) except commands
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_router))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
