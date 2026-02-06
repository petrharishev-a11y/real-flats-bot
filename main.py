import os
import re
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# =========================
# CONFIG (ENV VARS)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")  # without @
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()  # channel id like -100...

if not GROUP_CHAT_ID:
    GROUP_CHAT_ID_INT: Optional[int] = None
else:
    try:
        GROUP_CHAT_ID_INT = int(GROUP_CHAT_ID)
    except Exception:
        GROUP_CHAT_ID_INT = None

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("real-flats-bot")


# =========================
# DATA
# =========================
REQUEST_TTL_SECONDS = 48 * 3600  # 48h

@dataclass
class Request:
    rid: str
    author_id: int
    author_name: str
    created_at: float
    status: str = "active"  # active/closed

    districts: str = ""
    budget: str = ""
    rooms: str = ""
    bedrooms: str = ""
    amenities: str = ""
    area: str = ""
    comment: str = ""

    channel_message_id: Optional[int] = None
    agents_seen: Dict[int, str] = field(default_factory=dict)  # agent_id -> display


REQUESTS: Dict[str, Request] = {}
RID_COUNTER = 0


# =========================
# HELPERS
# =========================
def require_env():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if not BOT_USERNAME:
        raise RuntimeError("BOT_USERNAME is required (without @)")
    if GROUP_CHAT_ID_INT is None:
        raise RuntimeError("GROUP_CHAT_ID is required and must be integer (channel id like -100...)")

def next_rid() -> str:
    global RID_COUNTER
    RID_COUNTER += 1
    return f"R{RID_COUNTER:03d}"  # R001, R002 ...

def user_display(u) -> str:
    # show @username if exists, else name + id
    if not u:
        return "Unknown"
    if getattr(u, "username", None):
        return f"@{u.username}"
    fn = getattr(u, "first_name", "") or "User"
    return f"{fn} (id:{u.id})"

def deep_link_offer(rid: str) -> str:
    # Opens bot with offer context
    return f"https://t.me/{BOT_USERNAME}?start=offer_{rid}"

def deep_link_reply(rid: str, agent_id: int) -> str:
    # Opens bot with reply context to agent
    return f"https://t.me/{BOT_USERNAME}?start=reply_{rid}_{agent_id}"

def sanitize_text(s: str) -> str:
    return (s or "").strip()

def is_no(s: str) -> bool:
    s = (s or "").strip().lower()
    return s in {"нет", "no", "n", "0", "-", "none"}

def format_request_for_channel(req: Request) -> str:
    # No client identity here (privacy)
    lines = [
        f"🆕 *Новый запрос* `{req.rid}`",
        "",
        f"📍 *Районы:* {req.districts}",
        f"💰 *Бюджет:* {req.budget}",
        f"🏠 *Комнаты:* {req.rooms}",
        f"🛏 *Спальни:* {req.bedrooms}",
        f"🧰 *Удобства:* {req.amenities}",
        f"📐 *Площадь:* {req.area}",
    ]
    if req.comment and not is_no(req.comment):
        lines.append(f"💬 *Комментарий:* {req.comment}")
    lines += [
        "",
        "👇 Нажми кнопку ниже и отправь варианты боту (их увидит только клиент).",
    ]
    return "\n".join(lines)

def format_request_for_author(req: Request) -> str:
    lines = [
        f"✅ Запрос `{req.rid}` создан.",
        "",
        f"📍 Районы: {req.districts}",
        f"💰 Бюджет: {req.budget}",
        f"🏠 Комнаты: {req.rooms}",
        f"🛏 Спальни: {req.bedrooms}",
        f"🧰 Удобства: {req.amenities}",
        f"📐 Площадь: {req.area}",
    ]
    if req.comment and not is_no(req.comment):
        lines.append(f"💬 Комментарий: {req.comment}")
    lines.append("")
    lines.append("Если нужно — можешь дописать детали отдельным сообщением.")
    return "\n".join(lines)


# =========================
# CONVERSATION STATES
# =========================
(
    S_DISTRICTS,
    S_BUDGET,
    S_ROOMS,
    S_BEDROOMS,
    S_AMENITIES,
    S_AREA,
    S_COMMENT,
    S_CONFIRM,
) = range(8)


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep-link modes:
    # offer_R001  -> agent sends offers for request
    # reply_R001_8132... -> client replies to agent via bot
    args = context.args or []
    if args:
        payload = args[0].strip()
        if payload.startswith("offer_"):
            rid = payload.replace("offer_", "", 1).strip()
            return await start_offer_mode(update, context, rid)
        if payload.startswith("reply_"):
            rest = payload.replace("reply_", "", 1).strip()
            # reply_{rid}_{agent_id}
            m = re.match(r"^(R\d{3})_(\d+)$", rest)
            if m:
                rid = m.group(1)
                agent_id = int(m.group(2))
                return await start_reply_mode(update, context, rid, agent_id)

    text = (
        "Привет! Я бот Real Flats.\n\n"
        "Создать запрос: /request\n"
        "Мои активные запросы: /my\n"
        "Помощь: /help"
    )
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Как это работает:\n"
        "1) Клиент делает /request в личке с ботом.\n"
        "2) Бот публикует запрос в канал и ставит кнопку.\n"
        "3) Агент жмёт кнопку → бот открывается на нужном запросе → агент кидает варианты.\n"
        "4) Варианты видит только клиент.\n\n"
        "Команды:\n"
        "/request — создать запрос\n"
        "/my — мои активные запросы\n"
        "/cancel — отмена текущего действия\n"
    )
    await update.message.reply_text(text)

async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u:
        return
    mine = [r for r in REQUESTS.values() if r.author_id == u.id and r.status == "active"]
    if not mine:
        await update.message.reply_text("У тебя нет активных запросов.")
        return
    lines = ["Твои активные запросы:"]
    for r in mine:
        lines.append(f"- {r.rid}: {r.districts} | {r.budget} | {r.rooms}к | {r.bedrooms} сп")
    await update.message.reply_text("\n".join(lines))

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # cancel conversation or modes
    context.user_data.pop("mode", None)
    context.user_data.pop("offer_rid", None)
    context.user_data.pop("reply_rid", None)
    context.user_data.pop("reply_agent_id", None)
    await update.message.reply_text("Ок, отменил.")
    return ConversationHandler.END


# =========================
# REQUEST CREATION FLOW
# =========================
async def request_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"] = {}
    await update.message.reply_text("Ок, начнём.\n\n1) Какие районы? (можно несколько)")
    return S_DISTRICTS

async def request_districts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["districts"] = sanitize_text(update.message.text)
    await update.message.reply_text("2) Бюджет? (например: $800–1200)")
    return S_BUDGET

async def request_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["budget"] = sanitize_text(update.message.text)
    await update.message.reply_text("3) Комнаты? (например: 2к / 3к / студия)")
    return S_ROOMS

async def request_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["rooms"] = sanitize_text(update.message.text)
    await update.message.reply_text("4) Спальни? (например: 1 / 2 / 3)")
    return S_BEDROOMS

async def request_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["bedrooms"] = sanitize_text(update.message.text)
    await update.message.reply_text(
        "5) Удобства (критичные): посудомойка / ванна / духовка и т.д.\n"
        "Если неважно — напиши: нет"
    )
    return S_AMENITIES

async def request_amenities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["amenities"] = sanitize_text(update.message.text)
    await update.message.reply_text(
        "6) Желаемая площадь (м²)?\n"
        "Если нет — напиши: нет"
    )
    return S_AREA

async def request_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["area"] = sanitize_text(update.message.text)
    await update.message.reply_text(
        "7) Комментарий (если есть). Если нет — напиши: нет"
    )
    return S_COMMENT

async def request_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_req"]["comment"] = sanitize_text(update.message.text)

    data = context.user_data.get("new_req", {})
    preview = (
        "Проверь:\n\n"
        f"📍 Районы: {data.get('districts','')}\n"
        f"💰 Бюджет: {data.get('budget','')}\n"
        f"🏠 Комнаты: {data.get('rooms','')}\n"
        f"🛏 Спальни: {data.get('bedrooms','')}\n"
        f"🧰 Удобства: {data.get('amenities','')}\n"
        f"📐 Площадь: {data.get('area','')}\n"
        f"💬 Комментарий: {data.get('comment','')}\n\n"
        "Отправляем? (да/нет)"
    )
    await update.message.reply_text(preview)
    return S_CONFIRM

async def request_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = (update.message.text or "").strip().lower()
    if ans not in {"да", "yes", "y"}:
        await update.message.reply_text("Ок, не отправляю. Если нужно заново — /request")
        context.user_data.pop("new_req", None)
        return ConversationHandler.END

    u = update.effective_user
    if not u:
        await update.message.reply_text("Ошибка: не вижу пользователя.")
        return ConversationHandler.END

    data = context.user_data.get("new_req", {})
    rid = next_rid()

    req = Request(
        rid=rid,
        author_id=u.id,
        author_name=user_display(u),
        created_at=time.time(),
        districts=data.get("districts", ""),
        budget=data.get("budget", ""),
        rooms=data.get("rooms", ""),
        bedrooms=data.get("bedrooms", ""),
        amenities=data.get("amenities", ""),
        area=data.get("area", ""),
        comment=data.get("comment", ""),
    )
    REQUESTS[rid] = req
    context.user_data.pop("new_req", None)

    # Inform author
    await update.message.reply_text(format_request_for_author(req), parse_mode=ParseMode.MARKDOWN)

    # Post to channel with button
    channel_text = format_request_for_channel(req)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Отправить варианты по запросу", url=deep_link_offer(rid))
    ]])

    try:
        msg = await context.bot.send_message(
            chat_id=GROUP_CHAT_ID_INT,
            text=channel_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            disable_notification=True,
            reply_markup=kb,
        )
        req.channel_message_id = msg.message_id
    except Exception as e:
        log.exception("Failed to post to channel: %s", e)
        await update.message.reply_text("Запрос создан, но я не смог опубликовать его в канал. Проверь права бота в канале (admin).")

    return ConversationHandler.END


# =========================
# OFFER MODE (AGENTS)
# =========================
async def start_offer_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: str):
    rid = rid.strip().upper()
    if rid not in REQUESTS or REQUESTS[rid].status != "active":
        await update.message.reply_text("Этот запрос не найден или уже закрыт.")
        return

    context.user_data["mode"] = "offer"
    context.user_data["offer_rid"] = rid

    await update.message.reply_text(
        f"Ок, ты отправляешь варианты по запросу {rid}.\n"
        "Просто скидывай ссылки/описания сообщениями.\n"
        "Когда закончишь — напиши /done"
    )

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") == "offer":
        context.user_data.pop("mode", None)
        context.user_data.pop("offer_rid", None)
        await update.message.reply_text("Принято ✅")
        return
    if context.user_data.get("mode") == "reply":
        context.user_data.pop("mode", None)
        context.user_data.pop("reply_rid", None)
        context.user_data.pop("reply_agent_id", None)
        await update.message.reply_text("Ок ✅")
        return
    await update.message.reply_text("Нечего завершать.")

async def handle_offer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") != "offer":
        return
    rid = context.user_data.get("offer_rid")
    if not rid or rid not in REQUESTS:
        await update.message.reply_text("Запрос не найден. Нажми кнопку в канале заново.")
        context.user_data.pop("mode", None)
        context.user_data.pop("offer_rid", None)
        return

    req = REQUESTS[rid]
    if req.status != "active":
        await update.message.reply_text("Этот запрос уже закрыт.")
        return

    agent = update.effective_user
    if not agent:
        await update.message.reply_text("Не вижу отправителя.")
        return

    agent_disp = user_display(agent)
    req.agents_seen[agent.id] = agent_disp

    offer_text = sanitize_text(update.message.text)

    # message to client (author)
    client_text = (
        f"🏠 *Вариант по запросу* `{rid}`\n"
        f"👤 *Агент:* {agent_disp}\n\n"
        f"{offer_text}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ Ответить агенту", url=deep_link_reply(rid, agent.id))
    ]])

    try:
        await context.bot.send_message(
            chat_id=req.author_id,
            text=client_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        await update.message.reply_text("Отправлено клиенту ✅ Можешь кидать ещё или /done")
    except Exception as e:
        log.exception("Failed to send offer to client: %s", e)
        await update.message.reply_text("Не смог отправить клиенту (возможно клиент не запускал бота).")


# =========================
# REPLY MODE (CLIENT -> AGENT VIA BOT)
# =========================
async def start_reply_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: str, agent_id: int):
    rid = rid.strip().upper()
    if rid not in REQUESTS:
        await update.message.reply_text("Запрос не найден.")
        return

    context.user_data["mode"] = "reply"
    context.user_data["reply_rid"] = rid
    context.user_data["reply_agent_id"] = agent_id

    await update.message.reply_text(
        f"Ответ агенту по запросу {rid}.\n"
        "Напиши текст сообщением. Отменить: /cancel"
    )

async def handle_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") != "reply":
        return

    rid = context.user_data.get("reply_rid")
    agent_id = context.user_data.get("reply_agent_id")

    if not rid or not agent_id:
        await update.message.reply_text("Контекст ответа потерян. Нажми кнопку «Ответить агенту» ещё раз.")
        context.user_data.pop("mode", None)
        return

    req = REQUESTS.get(rid)
    if not req:
        await update.message.reply_text("Запрос уже не найден.")
        context.user_data.pop("mode", None)
        return

    txt = sanitize_text(update.message.text)
    sender = update.effective_user
    sender_disp = user_display(sender) if sender else "Клиент"

    out = (
        f"💬 *Сообщение по запросу* `{rid}`\n"
        f"От: {sender_disp}\n\n"
        f"{txt}"
    )

    try:
        await context.bot.send_message(
            chat_id=int(agent_id),
            text=out,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        await update.message.reply_text("Отправлено агенту ✅")
    except Exception as e:
        log.exception("Failed to send reply to agent: %s", e)
        await update.message.reply_text("Не смог отправить агенту. Возможно агент ещё не запускал бота.")

    context.user_data.pop("mode", None)
    context.user_data.pop("reply_rid", None)
    context.user_data.pop("reply_agent_id", None)


# =========================
# TTL WATCHER (optional)
# =========================
async def ttl_watcher(app: Application):
    # Reminds client that request is older than 48h (optional behavior)
    while True:
        now = time.time()
        for req in list(REQUESTS.values()):
            if req.status != "active":
                continue
            if now - req.created_at >= REQUEST_TTL_SECONDS:
                try:
                    await app.bot.send_message(
                        chat_id=req.author_id,
                        text=(
                            f"⏳ Запрос `{req.rid}` живёт уже 48 часов.\n"
                            "Если он ещё актуален — напиши /my и создай новый или просто продолжай принимать варианты.\n"
                            "Если не актуален — напиши /cancel."
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    # push created_at forward 1h to avoid spam
                    req.created_at = now + 3600
                except Exception:
                    pass
        await asyncio.sleep(600)


async def post_init(application: Application):
    # Start background watcher correctly (event loop already running)
    application.create_task(ttl_watcher(application))


# =========================
# MAIN
# =========================
def build_app() -> Application:
    require_env()

    conv = ConversationHandler(
        entry_points=[CommandHandler("request", request_start)],
        states={
            S_DISTRICTS: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_districts)],
            S_BUDGET: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_budget)],
            S_ROOMS: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_rooms)],
            S_BEDROOMS: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_bedrooms)],
            S_AMENITIES: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_amenities)],
            S_AREA: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_area)],
            S_COMMENT: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_comment)],
            S_CONFIRM: [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, request_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))

    app.add_handler(conv)

    # Offer / Reply message handlers (private only)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_offer_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_reply_message))

    return app


def main():
    app = build_app()
    log.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
