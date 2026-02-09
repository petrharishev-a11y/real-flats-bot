import os
import time
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID", "").strip()  # сюда ставим ID КАНАЛА (например -100...)
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is required")
if not GROUP_CHAT_ID_RAW:
    raise RuntimeError("GROUP_CHAT_ID is required (channel chat id)")
GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)

BOT_LINK = f"https://t.me/{BOT_USERNAME}"

# =========================
# CONFIG
# =========================
REMIND_EVERY_SECONDS = 2 * 24 * 60 * 60  # 2 дня
SESSION_TTL_SECONDS = 60 * 60            # 1 час "живой чат"

PRICE_LIMITS = [500, 800, 1000, 1300, 1500, 1800, 2000, 2500]  # #до500 ... #до2500, иначе #от2500

ROOM_TAGS = {"1": "#1к", "2": "#2к", "3": "#3к", "4": "#4к", "5": "#5к", "6": "#6к"}

DISTRICTS = [
    ("Ваке", "#ваке"),
    ("Вера", "#вера"),
    ("Сололаки", "#сололаки"),
    ("Старый город", "#старыйгород"),
    ("Сабуртало", "#сабуртало"),
    ("Чугурети", "#чугурети"),
    ("Дидубе", "#дидубе"),
    ("Церетели", "#церетели"),
    ("Ортачала", "#ортачала"),
    ("Дигоми массив", "#дигомимассив"),
    ("Диди Дигоми", "#дидидигоми"),
    ("Глдани", "#глдани"),
    ("Варкетили", "#варкетили"),
]
DISTRICT_BY_TEXT = {name.lower(): tag for name, tag in DISTRICTS}

YES_NO_KB = ReplyKeyboardMarkup([["ДА", "НЕТ"]], resize_keyboard=True, one_time_keyboard=True)

# =========================
# STORAGE (пока в памяти)
# =========================
REQ_COUNTER = 0  # R001, R002...
REQUESTS: Dict[str, "Request"] = {}

# активные "диалоги" в личке (чтобы можно было писать без кнопки каждый раз)
# user_id -> (peer_id, req_id, expires_at)
ACTIVE_CHAT: Dict[int, Tuple[int, str, float]] = {}


@dataclass
class Request:
    req_id: str
    author_id: int
    created_at: float
    last_remind_at: float = 0.0
    awaiting_remind_answer: bool = False

    district_name: str = ""
    district_tag: str = ""
    rooms: str = ""
    rooms_tag: str = ""
    budget: int = 0
    price_tag: str = ""
    bedrooms: Optional[str] = None

    dishwasher: Optional[bool] = None
    bath: Optional[bool] = None
    oven: Optional[bool] = None
    area_m2: Optional[int] = None
    comment: str = ""

    channel_message_id: Optional[int] = None
    is_active: bool = True


# =========================
# HELPERS
# =========================
def next_req_id() -> str:
    global REQ_COUNTER
    REQ_COUNTER += 1
    return f"R{REQ_COUNTER:03d}"


def pick_price_tag(price: int) -> str:
    for lim in PRICE_LIMITS:
        if price <= lim:
            return f"#до{lim}"
    return "#от2500"


def normalize_yes_no(text: str) -> Optional[bool]:
    t = (text or "").strip().lower()
    if t in ("да", "yes", "y", "+"):
        return True
    if t in ("нет", "no", "n", "-"):
        return False
    return None


def build_tags_line(req: Request) -> str:
    return f"{req.price_tag} {req.rooms_tag} {req.district_tag}"


def req_public_text(req: Request) -> str:
    # Пост в канале — без автора (анонимно)
    lines = [
        f"🟢 Запрос #{req.req_id}",
        "",
        f"📍 Район: {req.district_name}",
        f"🚪 Комнаты: {req.rooms}",
        f"💵 Бюджет: ${req.budget}",
    ]
    if req.bedrooms:
        lines.append(f"🛏 Спальни: {req.bedrooms}")

    # Удобства
    def yn(v: Optional[bool]) -> str:
        return "ДА" if v is True else "НЕТ" if v is False else "—"

    lines += [
        "",
        f"🧰 Удобства:",
        f"• Посудомойка: {yn(req.dishwasher)}",
        f"• Ванна: {yn(req.bath)}",
        f"• Духовка: {yn(req.oven)}",
    ]

    if req.area_m2 is not None:
        lines.append(f"📐 Площадь: {req.area_m2} м²")

    if req.comment and req.comment.strip().lower() != "нет":
        lines += ["", f"💬 Комментарий: {req.comment.strip()}"]

    lines += [
        "",
        "—",
        build_tags_line(req),
    ]
    return "\n".join(lines)


async def delete_request_everywhere(req: Request, context: ContextTypes.DEFAULT_TYPE, reason: str = ""):
    req.is_active = False

    # удаляем пост в канале
    if req.channel_message_id is not None:
        try:
            await context.bot.delete_message(chat_id=GROUP_CHAT_ID, message_id=req.channel_message_id)
        except Exception:
            pass

    # уведомим автора
    try:
        msg = f"🧹 Запрос #{req.req_id} удалён."
        if reason:
            msg += f"\nПричина: {reason}"
        await context.bot.send_message(chat_id=req.author_id, text=msg)
    except Exception:
        pass

    # чистим память
    REQUESTS.pop(req.req_id, None)


def set_active_chat(user_id: int, peer_id: int, req_id: str):
    ACTIVE_CHAT[user_id] = (peer_id, req_id, time.time() + SESSION_TTL_SECONDS)


def get_active_chat(user_id: int) -> Optional[Tuple[int, str]]:
    data = ACTIVE_CHAT.get(user_id)
    if not data:
        return None
    peer_id, req_id, exp = data
    if time.time() > exp:
        ACTIVE_CHAT.pop(user_id, None)
        return None
    return peer_id, req_id


# =========================
# /start deep links
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args:
        payload = args[0].strip()
        if payload.startswith("reply_"):
            req_id = payload.replace("reply_", "", 1).strip()
            req = REQUESTS.get(req_id)
            if not req or not req.is_active:
                await update.message.reply_text("Запрос не найден или уже закрыт.")
                return
            # агентский режим: писать варианты
            context.user_data["mode"] = "agent_reply"
            context.user_data["req_id"] = req_id
            await update.message.reply_text(
                f"Отправляй варианты по запросу #{req_id} (ссылки/текст). "
                f"Когда закончишь — напиши: ГОТОВО",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        if payload.startswith("view_"):
            req_id = payload.replace("view_", "", 1).strip()
            req = REQUESTS.get(req_id)
            if not req:
                await update.message.reply_text("Запрос не найден.")
                return
            await update.message.reply_text(req_public_text(req))
            return

    await update.message.reply_text(
        "Привет! Команды:\n"
        "/request — создать запрос\n"
        "/my — мои активные запросы\n"
        "/help — помощь"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/request — создать запрос\n"
        "/my — мои активные запросы\n\n"
        "Агентам: отвечайте на запросы через кнопку в канале «Отправить варианты»."
    )


# =========================
# REQUEST FLOW
# =========================
(
    ST_DISTRICT,
    ST_ROOMS,
    ST_BUDGET,
    ST_BEDROOMS,
    ST_DISHWASHER,
    ST_BATH,
    ST_OVEN,
    ST_AREA,
    ST_COMMENT,
) = range(9)


def district_keyboard():
    rows = []
    row = []
    for i, (name, _tag) in enumerate(DISTRICTS, start=1):
        row.append(name)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def rooms_keyboard():
    return ReplyKeyboardMarkup([["1", "2", "3"], ["4", "5", "6"]], resize_keyboard=True, one_time_keyboard=True)


async def request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Создавать запрос нужно в личке с ботом.")
        return ConversationHandler.END

    context.user_data["req_draft"] = {}
    await update.message.reply_text("📍 Выбери район (ОДИН):", reply_markup=district_keyboard())
    return ST_DISTRICT


async def st_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    tag = DISTRICT_BY_TEXT.get(text.lower())
    if not tag:
        await update.message.reply_text("Выбери район кнопкой ниже:", reply_markup=district_keyboard())
        return ST_DISTRICT

    context.user_data["req_draft"]["district_name"] = text
    context.user_data["req_draft"]["district_tag"] = tag
    await update.message.reply_text("🚪 Сколько комнат? (1–6)", reply_markup=rooms_keyboard())
    return ST_ROOMS


async def st_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text not in ROOM_TAGS:
        await update.message.reply_text("Выбери 1–6 кнопкой:", reply_markup=rooms_keyboard())
        return ST_ROOMS
    context.user_data["req_draft"]["rooms"] = text
    context.user_data["req_draft"]["rooms_tag"] = ROOM_TAGS[text]
    await update.message.reply_text("💵 Бюджет в $ (только число, например 1200):", reply_markup=ReplyKeyboardRemove())
    return ST_BUDGET


async def st_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.search(r"\d+", text)
    if not m:
        await update.message.reply_text("Напиши число, например: 1200")
        return ST_BUDGET
    budget = int(m.group(0))
    context.user_data["req_draft"]["budget"] = budget
    context.user_data["req_draft"]["price_tag"] = pick_price_tag(budget)

    await update.message.reply_text("🛏 Сколько спален? (или напиши НЕТ)")
    return ST_BEDROOMS


async def st_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() == "нет":
        context.user_data["req_draft"]["bedrooms"] = None
    else:
        if not re.fullmatch(r"\d+", text):
            await update.message.reply_text("Напиши число (например 1/2) или НЕТ")
            return ST_BEDROOMS
        context.user_data["req_draft"]["bedrooms"] = text

    await update.message.reply_text("🧰 Посудомойка обязательна? (ДА/НЕТ)", reply_markup=YES_NO_KB)
    return ST_DISHWASHER


async def st_dishwasher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = normalize_yes_no(update.message.text)
    if v is None:
        await update.message.reply_text("Ответь ДА или НЕТ", reply_markup=YES_NO_KB)
        return ST_DISHWASHER
    context.user_data["req_draft"]["dishwasher"] = v

    await update.message.reply_text("🛁 Ванна обязательна? (ДА/НЕТ)", reply_markup=YES_NO_KB)
    return ST_BATH


async def st_bath(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = normalize_yes_no(update.message.text)
    if v is None:
        await update.message.reply_text("Ответь ДА или НЕТ", reply_markup=YES_NO_KB)
        return ST_BATH
    context.user_data["req_draft"]["bath"] = v

    await update.message.reply_text("🍽 Духовка обязательна? (ДА/НЕТ)", reply_markup=YES_NO_KB)
    return ST_OVEN


async def st_oven(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = normalize_yes_no(update.message.text)
    if v is None:
        await update.message.reply_text("Ответь ДА или НЕТ", reply_markup=YES_NO_KB)
        return ST_OVEN
    context.user_data["req_draft"]["oven"] = v

    await update.message.reply_text("📐 Желаемая площадь (м²) или НЕТ:", reply_markup=ReplyKeyboardRemove())
    return ST_AREA


async def st_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() == "нет":
        context.user_data["req_draft"]["area_m2"] = None
    else:
        m = re.search(r"\d+", text)
        if not m:
            await update.message.reply_text("Напиши число (например 65) или НЕТ")
            return ST_AREA
        context.user_data["req_draft"]["area_m2"] = int(m.group(0))

    await update.message.reply_text("💬 Комментарий (или НЕТ):")
    return ST_COMMENT


async def st_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    context.user_data["req_draft"]["comment"] = text

    # Создаём запрос
    req_id = next_req_id()
    d = context.user_data.get("req_draft", {})

    req = Request(
        req_id=req_id,
        author_id=update.effective_user.id,
        created_at=time.time(),
        district_name=d["district_name"],
        district_tag=d["district_tag"],
        rooms=d["rooms"],
        rooms_tag=d["rooms_tag"],
        budget=d["budget"],
        price_tag=d["price_tag"],
        bedrooms=d.get("bedrooms"),
        dishwasher=d.get("dishwasher"),
        bath=d.get("bath"),
        oven=d.get("oven"),
        area_m2=d.get("area_m2"),
        comment=d.get("comment", ""),
    )
    REQUESTS[req_id] = req

    # Постим в канал + кнопка агентам
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📩 Отправить варианты", url=f"{BOT_LINK}?start=reply_{req_id}")],
            [InlineKeyboardButton("🔎 Открыть в боте", url=f"{BOT_LINK}?start=view_{req_id}")],
        ]
    )
    msg = await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=req_public_text(req),
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    req.channel_message_id = msg.message_id

    await update.message.reply_text(
        f"✅ Запрос #{req_id} создан и опубликован в канале.\n"
        "Ответы агентов будут приходить сюда в личку.\n\n"
        "Команда: /my — посмотреть активные запросы",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("req_draft", None)
    await update.message.reply_text("Ок, отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    active = [r for r in REQUESTS.values() if r.author_id == uid and r.is_active]
    if not active:
        await update.message.reply_text("У тебя нет активных запросов.")
        return
    lines = ["Твои активные запросы:"]
    for r in sorted(active, key=lambda x: x.created_at, reverse=True):
        lines.append(f"• #{r.req_id} — {r.district_name}, {r.rooms}к, ${r.budget}")
    await update.message.reply_text("\n".join(lines))


# =========================
# CHAT / REPLIES
# =========================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    # 1) если это агентский режим (ответ по запросу)
    mode = context.user_data.get("mode")
    if mode == "agent_reply":
        req_id = context.user_data.get("req_id")
        if text.lower() == "готово":
            context.user_data.pop("mode", None)
            context.user_data.pop("req_id", None)
            await update.message.reply_text("✅ Ок, закончили. Если нужно — отвечай на другой запрос из канала.")
            return

        req = REQUESTS.get(req_id)
        if not req or not req.is_active:
            await update.message.reply_text("Запрос уже закрыт или удалён.")
            context.user_data.pop("mode", None)
            context.user_data.pop("req_id", None)
            return

        agent = update.effective_user
        agent_name = agent.username and f"@{agent.username}" or agent.first_name or "Агент"

        # отправляем автору запроса
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✉️ Ответить агенту", callback_data=f"chat|{req_id}|{uid}")],
            [InlineKeyboardButton("🧹 Закрыть запрос", callback_data=f"close|{req_id}")],
        ])
        await context.bot.send_message(
            chat_id=req.author_id,
            text=f"📩 Вариант по запросу #{req_id} от {agent_name}:\n\n{text}",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

        # ставим сессию “чат” для автора и агента на 1 час
        set_active_chat(req.author_id, uid, req_id)
        set_active_chat(uid, req.author_id, req_id)

        await update.message.reply_text("✅ Отправлено автору запроса.")
        return

    # 2) если есть активная сессия чата — пересылаем “как есть”
    active = get_active_chat(uid)
    if active:
        peer_id, req_id = active
        await context.bot.send_message(
            chat_id=peer_id,
            text=f"💬 Сообщение по #{req_id}:\n{text}",
            disable_web_page_preview=True,
        )
        # продлеваем сессию
        set_active_chat(uid, peer_id, req_id)
        return

    # 3) иначе подсказка
    await update.message.reply_text(
        "Я не понял, куда это отправить.\n\n"
        "Если ты агент — зайди в канал и нажми «📩 Отправить варианты» под нужным запросом.\n"
        "Если ты клиент/автор — нажми «✉️ Ответить агенту» под сообщением агента (или напиши /my)."
    )


# =========================
# CALLBACKS
# =========================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    parts = data.split("|")
    if not parts:
        return

    if parts[0] == "chat" and len(parts) == 3:
        req_id = parts[1]
        peer_id = int(parts[2])
        # включаем "сессию" на 1 час — теперь можно писать без кнопки
        set_active_chat(q.from_user.id, peer_id, req_id)
        await q.edit_message_reply_markup(reply_markup=q.message.reply_markup)
        await q.message.reply_text(f"✅ Чат активирован по #{req_id} на 1 час. Пиши сюда — я буду пересылать.")
        return

    if parts[0] == "close" and len(parts) == 2:
        req_id = parts[1]
        req = REQUESTS.get(req_id)
        if not req:
            await q.message.reply_text("Запрос не найден.")
            return
        if q.from_user.id != req.author_id:
            await q.message.reply_text("Закрыть запрос может только автор.")
            return
        await delete_request_everywhere(req, context, reason="закрыт автором")
        return

    if parts[0] == "keep" and len(parts) == 2:
        req_id = parts[1]
        req = REQUESTS.get(req_id)
        if not req:
            return
        if q.from_user.id != req.author_id:
            return
        req.last_remind_at = time.time()
        req.awaiting_remind_answer = False
        await q.message.reply_text(f"✅ Ок, запрос #{req_id} остаётся активным.")
        return

    if parts[0] == "drop" and len(parts) == 2:
        req_id = parts[1]
        req = REQUESTS.get(req_id)
        if not req:
            return
        if q.from_user.id != req.author_id:
            return
        await delete_request_everywhere(req, context, reason="автор подтвердил, что поиск не актуален")
        return


# =========================
# PERIODIC JOBS
# =========================
async def periodic_maintenance(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()

    # чистим истёкшие чаты
    to_del = []
    for uid, (_peer, _req_id, exp) in ACTIVE_CHAT.items():
        if now > exp:
            to_del.append(uid)
    for uid in to_del:
        ACTIVE_CHAT.pop(uid, None)

    # напоминания каждые 2 дня
    for req in list(REQUESTS.values()):
        if not req.is_active:
            continue
        if req.awaiting_remind_answer:
            # уже ждём ответ — не спамим
            continue
        if req.last_remind_at == 0:
            # первая точка отсчёта — от создания
            base = req.created_at
        else:
            base = req.last_remind_at

        if now - base >= REMIND_EVERY_SECONDS:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, ещё ищу", callback_data=f"keep|{req.req_id}")],
                [InlineKeyboardButton("❌ Нет, не актуально", callback_data=f"drop|{req.req_id}")],
            ])
            try:
                await context.bot.send_message(
                    chat_id=req.author_id,
                    text=f"⏰ Запрос #{req.req_id} ещё актуален?\n\nНажми ДА или НЕТ.",
                    reply_markup=kb,
                )
                req.awaiting_remind_answer = True
                req.last_remind_at = now
            except Exception:
                pass


# =========================
# MAIN
# =========================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("my", my_cmd))

    # /request conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("request", request_cmd)],
        states={
            ST_DISTRICT: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_district)],
            ST_ROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_rooms)],
            ST_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_budget)],
            ST_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_bedrooms)],
            ST_DISHWASHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_dishwasher)],
            ST_BATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_bath)],
            ST_OVEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_oven)],
            ST_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_area)],
            ST_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # callbacks
    app.add_handler(CallbackQueryHandler(callbacks))

    # текстовый роутер (после всего)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    return app


async def post_init(application: Application):
    # job_queue будет НЕ None только если requirements с [job-queue]
    if application.job_queue:
        application.job_queue.run_repeating(periodic_maintenance, interval=60, first=10)


def main():
    app = build_app()
    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
