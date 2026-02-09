import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
TARGET_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID", "").strip()  # теперь сюда кладём chat_id канала (минусовый -100...)
TARGET_CHAT_ID = int(TARGET_CHAT_ID_RAW) if TARGET_CHAT_ID_RAW else 0

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is required")
if not TARGET_CHAT_ID:
    raise RuntimeError("GROUP_CHAT_ID (channel id) is required")

BOT_LINK = f"https://t.me/{BOT_USERNAME}"

# =========================
# CONFIG
# =========================
REQUEST_TTL_SECONDS = 48 * 3600          # запрос живёт 48 часов (потом спросим актуальность)
REMIND_EVERY_SECONDS = 48 * 3600         # напоминание раз в 2 дня
CHAT_SESSION_TTL_SECONDS = 60 * 60       # чат агент↔автор активен 1 час
MAINTENANCE_INTERVAL_SECONDS = 120       # раз в 2 минуты чистим/проверяем

PRICE_TAGS = [
    (500,  "#до500"),
    (800,  "#до800"),
    (1000, "#до1000"),
    (1300, "#до1300"),
    (1500, "#до1500"),
    (1800, "#до1800"),
    (2000, "#до2000"),
    (2500, "#до2500"),
]
PRICE_TAG_OVER = "#от2500"

ROOM_TAGS = {1: "#1к", 2: "#2к", 3: "#3к", 4: "#4к", 5: "#5к", 6: "#6к"}

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
DISTRICT_BY_NAME = {n.lower(): (n, tag) for n, tag in DISTRICTS}

AMENITIES = [
    ("Посудомойка", "dishwasher"),
    ("Ванна", "bath"),
    ("Духовка", "oven"),
]

# =========================
# IN-MEMORY STORAGE
# =========================
NEXT_REQ_NUM = 1


@dataclass
class Request:
    req_id: str
    author_id: int
    author_username: str
    created_at: float
    last_remind_at: float
    awaiting_remind_answer: bool = False
    status: str = "active"  # active/closed
    channel_msg_id: Optional[int] = None

    districts: List[str] = field(default_factory=list)
    district_tags: List[str] = field(default_factory=list)

    rooms_min: int = 0
    rooms_max: int = 0
    room_tags: List[str] = field(default_factory=list)

    budget_min: int = 0
    budget_max: int = 0
    price_tags: List[str] = field(default_factory=list)

    bedrooms: Optional[int] = None
    pets: str = "Не важно"  # Да/Нет/Не важно

    amenities_required: List[str] = field(default_factory=list)  # ["dishwasher","bath","oven"]
    area_m2: Optional[int] = None
    comment: str = ""


REQUESTS: Dict[str, Request] = {}


@dataclass
class ActiveChat:
    peer_id: int
    req_id: str
    expires_at: float


ACTIVE_CHATS: Dict[int, ActiveChat] = {}  # user_id -> ActiveChat


# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return time.time()


def normalize_username(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("@"):
        return u
    return "@" + u


def pick_price_tag(amount: int) -> str:
    for limit, tag in PRICE_TAGS:
        if amount <= limit:
            return tag
    return PRICE_TAG_OVER


def price_tags_for_range(a: int, b: int) -> List[str]:
    t1 = pick_price_tag(a)
    t2 = pick_price_tag(b)
    tags = []
    for t in (t1, t2):
        if t not in tags:
            tags.append(t)
    return tags


def room_tags_for_range(rmin: int, rmax: int) -> List[str]:
    tags = []
    for r in range(rmin, rmax + 1):
        tag = ROOM_TAGS.get(r)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_tags_line(req: Request) -> str:
    tags = []
    for t in req.price_tags + req.room_tags + req.district_tags:
        if t not in tags:
            tags.append(t)
    return " ".join(tags).strip()


def amenities_human(req: Request) -> str:
    if not req.amenities_required:
        return "нет требований"
    mapping = {k: n for n, k in AMENITIES}
    return ", ".join(mapping.get(k, k) for k in req.amenities_required)


def request_public_text(req: Request) -> str:
    districts_txt = ", ".join(req.districts) if req.districts else "—"
    rooms_txt = f"{req.rooms_min}" if req.rooms_min == req.rooms_max else f"{req.rooms_min}–{req.rooms_max}"
    budget_txt = f"${req.budget_max}" if req.budget_min == req.budget_max else f"${req.budget_min}–${req.budget_max}"
    bedrooms_txt = str(req.bedrooms) if req.bedrooms is not None else "не важно"
    area_txt = f"{req.area_m2} м²" if req.area_m2 else "не важно"

    base = [
        f"🟠 Запрос #{req.req_id}",
        f"📍 Районы: {districts_txt}",
        f"🚪 Комнаты: {rooms_txt}",
        f"💰 Бюджет: {budget_txt}",
        f"🛏 Спален: {bedrooms_txt}",
        f"🐾 Животные: {req.pets}",
        f"🧰 Удобства: {amenities_human(req)}",
        f"📐 Площадь: {area_txt}",
    ]
    if req.comment.strip():
        base.append(f"💬 Комментарий: {req.comment.strip()}")
    tags = build_tags_line(req)
    if tags:
        base.append("")
        base.append(tags)
    return "\n".join(base)


def set_active_chat(user_id: int, peer_id: int, req_id: str) -> None:
    ACTIVE_CHATS[user_id] = ActiveChat(peer_id=peer_id, req_id=req_id, expires_at=now_ts() + CHAT_SESSION_TTL_SECONDS)


def get_active_chat(user_id: int) -> Optional[ActiveChat]:
    ac = ACTIVE_CHATS.get(user_id)
    if not ac:
        return None
    if ac.expires_at < now_ts():
        ACTIVE_CHATS.pop(user_id, None)
        return None
    return ac


def clear_active_chat(user_id: int) -> None:
    ACTIVE_CHATS.pop(user_id, None)


def make_req_id() -> str:
    global NEXT_REQ_NUM
    rid = f"R{NEXT_REQ_NUM:03d}"
    NEXT_REQ_NUM += 1
    return rid


async def delete_request_everywhere(app: Application, req: Request, reason: str = "") -> None:
    # delete message in channel if exists
    try:
        if req.channel_msg_id:
            await app.bot.delete_message(chat_id=TARGET_CHAT_ID, message_id=req.channel_msg_id)
    except Exception:
        pass

    # notify author
    try:
        txt = f"🧹 Запрос #{req.req_id} удалён."
        if reason:
            txt += f"\nПричина: {reason}"
        await app.bot.send_message(chat_id=req.author_id, text=txt)
    except Exception:
        pass

    # remove from store
    REQUESTS.pop(req.req_id, None)


def districts_keyboard() -> ReplyKeyboardMarkup:
    # кнопки районов + ГОТОВО/СБРОС
    rows = []
    row = []
    for i, (name, _) in enumerate(DISTRICTS, start=1):
        row.append(name)
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(["ГОТОВО", "СБРОС"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def amenities_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["Посудомойка", "Ванна", "Духовка"],
        ["ГОТОВО", "СБРОС", "НЕТ"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def pets_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["Да", "Нет", "Не важно"]], resize_keyboard=True, one_time_keyboard=True)


def rooms_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["1", "2", "3"],
            ["1-2", "2-3", "3-4"],
            ["4", "5", "6"],
            ["4-5", "5-6"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# =========================
# CONVERSATION STATES
# =========================
(
    ST_DISTRICTS,
    ST_ROOMS,
    ST_BUDGET,
    ST_BEDROOMS,
    ST_PETS,
    ST_AMENITIES,
    ST_AREA,
    ST_COMMENT,
) = range(8)


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args:
        payload = args[0].strip()
        # agent: deep-link from channel post
        if payload.startswith("reply_"):
            req_id = payload.replace("reply_", "", 1).strip()
            req = REQUESTS.get(req_id)
            if not req or req.status != "active":
                await update.message.reply_text("Этот запрос уже неактивен.", reply_markup=ReplyKeyboardRemove())
                return

            # mark that this user is replying to this req
            context.user_data["mode"] = "agent_reply"
            context.user_data["reply_req_id"] = req_id

            await update.message.reply_text(
                f"✅ Ты отвечаешь на запрос #{req_id}.\n"
                f"Просто отправь сюда ссылки/текст с вариантами.\n"
                f"Чтобы остановиться — напиши: ГОТОВО",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        # author: optional view
        if payload.startswith("view_"):
            req_id = payload.replace("view_", "", 1).strip()
            req = REQUESTS.get(req_id)
            if not req:
                await update.message.reply_text("Запрос не найден.")
                return
            await update.message.reply_text(request_public_text(req), disable_web_page_preview=True)
            return

    await update.message.reply_text(
        "Привет! Я бот Real Flats.\n\n"
        "Создать запрос: /request\n"
        "Мои активные запросы: /my\n"
        "Помощь: /help",
        reply_markup=ReplyKeyboardRemove(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/request — создать запрос\n"
        "/my — мои активные запросы (и закрыть)\n"
        "/help — помощь\n\n"
        "Если ты агент: переходи из канала по кнопке «Отправить варианты».",
    )


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    active = [r for r in REQUESTS.values() if r.author_id == uid and r.status == "active"]
    if not active:
        await update.message.reply_text("У тебя нет активных запросов.")
        return

    for r in sorted(active, key=lambda x: x.created_at, reverse=True):
        districts_txt = ", ".join(r.districts) if r.districts else "—"
        rooms_txt = f"{r.rooms_min}" if r.rooms_min == r.rooms_max else f"{r.rooms_min}–{r.rooms_max}"
        budget_txt = f"${r.budget_max}" if r.budget_min == r.budget_max else f"${r.budget_min}–${r.budget_max}"
        txt = f"🟠 #{r.req_id}\n📍 {districts_txt}\n🚪 {rooms_txt}\n💰 {budget_txt}"

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🧹 Закрыть запрос", callback_data=f"close|{r.req_id}")],
            ]
        )
        await update.message.reply_text(txt, reply_markup=kb)


# =========================
# REQUEST FLOW
# =========================
async def request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # only private
    if update.effective_chat.type != "private":
        await update.message.reply_text("Создавать запрос нужно в личке с ботом.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["selected_districts"] = []
    context.user_data["selected_district_tags"] = []

    await update.message.reply_text(
        "1) Какие районы нужны? (можно несколько)\n"
        "Нажимай районы кнопками, потом нажми «ГОТОВО».",
        reply_markup=districts_keyboard(),
    )
    return ST_DISTRICTS


async def st_districts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    selected: List[str] = context.user_data.get("selected_districts", [])
    selected_tags: List[str] = context.user_data.get("selected_district_tags", [])

    if text.upper() == "СБРОС":
        context.user_data["selected_districts"] = []
        context.user_data["selected_district_tags"] = []
        await update.message.reply_text("Ок, сбросил. Выбирай районы заново.", reply_markup=districts_keyboard())
        return ST_DISTRICTS

    if text.upper() == "ГОТОВО":
        if not selected:
            await update.message.reply_text("Нужно выбрать хотя бы один район.", reply_markup=districts_keyboard())
            return ST_DISTRICTS

        context.user_data["districts"] = selected
        context.user_data["district_tags"] = selected_tags

        await update.message.reply_text(
            "2) Сколько комнат?\nМожно одно число (2) или диапазон (2-3).",
            reply_markup=rooms_keyboard(),
        )
        return ST_ROOMS

    key = text.lower()
    if key in DISTRICT_BY_NAME:
        name, tag = DISTRICT_BY_NAME[key]
        if name not in selected:
            selected.append(name)
        if tag not in selected_tags:
            selected_tags.append(tag)
        context.user_data["selected_districts"] = selected
        context.user_data["selected_district_tags"] = selected_tags
        await update.message.reply_text(
            f"Добавил: {name}\nВыбери ещё или нажми «ГОТОВО».",
            reply_markup=districts_keyboard(),
        )
        return ST_DISTRICTS

    await update.message.reply_text("Не понял. Выбери район кнопкой или нажми «ГОТОВО».", reply_markup=districts_keyboard())
    return ST_DISTRICTS


def parse_rooms(text: str) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"\s*(\d)\s*(?:[-–]\s*(\d)\s*)?\s*", text)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if a > b:
        a, b = b, a
    if a < 1 or b > 6:
        return None
    return a, b


async def st_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    parsed = parse_rooms(text)
    if not parsed:
        await update.message.reply_text("Формат: 2 или 2-3. Диапазон 1–6.", reply_markup=rooms_keyboard())
        return ST_ROOMS

    rmin, rmax = parsed
    context.user_data["rooms_min"] = rmin
    context.user_data["rooms_max"] = rmax
    context.user_data["room_tags"] = room_tags_for_range(rmin, rmax)

    await update.message.reply_text(
        "3) Бюджет ($)?\nМожно одно число (1200) или диапазон (800-1200).",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ST_BUDGET


def parse_budget(text: str) -> Optional[Tuple[int, int]]:
    nums = re.findall(r"\d+", text.replace(" ", ""))
    if not nums:
        return None
    if len(nums) == 1:
        a = b = int(nums[0])
    else:
        a = int(nums[0])
        b = int(nums[1])
    if a > b:
        a, b = b, a
    if a <= 0 or b <= 0:
        return None
    return a, b


async def st_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    parsed = parse_budget(text)
    if not parsed:
        await update.message.reply_text("Формат: 1200 или 800-1200.")
        return ST_BUDGET

    bmin, bmax = parsed
    context.user_data["budget_min"] = bmin
    context.user_data["budget_max"] = bmax
    context.user_data["price_tags"] = price_tags_for_range(bmin, bmax)

    await update.message.reply_text(
        "4) Сколько спален?\nНапиши число (0/1/2/3...) или «не важно».",
    )
    return ST_BEDROOMS


async def st_bedrooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().lower()
    if text in ("не важно", "неважно", "-", "нет"):
        context.user_data["bedrooms"] = None
    else:
        if not re.fullmatch(r"\d+", text):
            await update.message.reply_text("Напиши число (например 1) или «не важно».")
            return ST_BEDROOMS
        context.user_data["bedrooms"] = int(text)

    await update.message.reply_text("5) Животные допустимы?", reply_markup=pets_keyboard())
    return ST_PETS


async def st_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text not in ("Да", "Нет", "Не важно"):
        await update.message.reply_text("Выбери: Да / Нет / Не важно", reply_markup=pets_keyboard())
        return ST_PETS
    context.user_data["pets"] = text

    context.user_data["amenities_selected"] = []
    await update.message.reply_text(
        "6) Нужны ли какие-то удобства? (можно несколько)\n"
        "Нажимай удобства, потом «ГОТОВО». Если не важно — «НЕТ».",
        reply_markup=amenities_keyboard(),
    )
    return ST_AMENITIES


async def st_amenities(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    selected: List[str] = context.user_data.get("amenities_selected", [])

    if text.upper() == "СБРОС":
        context.user_data["amenities_selected"] = []
        await update.message.reply_text("Сбросил. Выбирай заново.", reply_markup=amenities_keyboard())
        return ST_AMENITIES

    if text.upper() == "НЕТ":
        context.user_data["amenities_required"] = []
        await update.message.reply_text(
            "7) Желаемая площадь (м²)?\nНапиши число или «нет».",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ST_AREA

    if text.upper() == "ГОТОВО":
        context.user_data["amenities_required"] = selected
        await update.message.reply_text(
            "7) Желаемая площадь (м²)?\nНапиши число или «нет».",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ST_AREA

    # toggle add
    key = None
    for human, code in AMENITIES:
        if text == human:
            key = code
            break

    if not key:
        await update.message.reply_text("Выбери удобство кнопкой или нажми «ГОТОВО/НЕТ».", reply_markup=amenities_keyboard())
        return ST_AMENITIES

    if key not in selected:
        selected.append(key)
        context.user_data["amenities_selected"] = selected

    await update.message.reply_text("Ок. Добавил. Можно ещё или «ГОТОВО».", reply_markup=amenities_keyboard())
    return ST_AMENITIES


async def st_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().lower()
    if text in ("нет", "не важно", "неважно", "-", ""):
        context.user_data["area_m2"] = None
    else:
        m = re.findall(r"\d+", text)
        if not m:
            await update.message.reply_text("Напиши число (например 70) или «нет».")
            return ST_AREA
        context.user_data["area_m2"] = int(m[0])

    await update.message.reply_text("8) Комментарий (если есть). Если нет — напиши «нет».")
    return ST_COMMENT


async def st_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text.lower() in ("нет", "не важно", "неважно", "-"):
        text = ""
    context.user_data["comment"] = text

    # create request
    u = update.effective_user
    req_id = make_req_id()

    districts = context.user_data["districts"]
    district_tags = context.user_data["district_tags"]
    rooms_min = context.user_data["rooms_min"]
    rooms_max = context.user_data["rooms_max"]
    room_tags = context.user_data["room_tags"]
    budget_min = context.user_data["budget_min"]
    budget_max = context.user_data["budget_max"]
    price_tags = context.user_data["price_tags"]
    bedrooms = context.user_data["bedrooms"]
    pets = context.user_data["pets"]
    amenities_required = context.user_data.get("amenities_required", [])
    area_m2 = context.user_data["area_m2"]

    req = Request(
        req_id=req_id,
        author_id=u.id,
        author_username=normalize_username(u.username or u.first_name or ""),
        created_at=now_ts(),
        last_remind_at=now_ts(),
        districts=districts,
        district_tags=district_tags,
        rooms_min=rooms_min,
        rooms_max=rooms_max,
        room_tags=room_tags,
        budget_min=budget_min,
        budget_max=budget_max,
        price_tags=price_tags,
        bedrooms=bedrooms,
        pets=pets,
        amenities_required=amenities_required,
        area_m2=area_m2,
        comment=text,
    )
    REQUESTS[req_id] = req

    # post to channel
    post_text = request_public_text(req)

    # button: agent goes to bot with request id
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📩 Отправить варианты", url=f"{BOT_LINK}?start=reply_{req_id}")],
        ]
    )
    msg = await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=post_text,
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    req.channel_msg_id = msg.message_id

    # notify author with close button
    close_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧹 Закрыть запрос", callback_data=f"close|{req_id}")]])
    await update.message.reply_text(
        f"✅ Запрос создан: #{req_id}\nОн опубликован в канале.\n\n"
        f"Если нужно — закрой его кнопкой ниже или командой /my.",
        reply_markup=close_kb,
    )
    return ConversationHandler.END


# =========================
# CALLBACKS
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if data.startswith("close|"):
        req_id = data.split("|", 1)[1].strip()
        req = REQUESTS.get(req_id)
        if not req:
            await q.edit_message_text("Запрос уже удалён.")
            return
        if q.from_user.id != req.author_id:
            await q.edit_message_text("Закрыть запрос может только автор.")
            return

        await delete_request_everywhere(context.application, req, reason="закрыт автором")
        try:
            await q.edit_message_text(f"🧹 Запрос #{req_id} закрыт и удалён.")
        except Exception:
            pass
        return

    if data.startswith("keep|") or data.startswith("drop|"):
        action, req_id = data.split("|", 1)
        req = REQUESTS.get(req_id)
        if not req:
            await q.edit_message_text("Запрос уже удалён.")
            return
        if q.from_user.id != req.author_id:
            await q.edit_message_text("Ответить может только автор запроса.")
            return

        if action == "keep":
            req.last_remind_at = now_ts()
            req.awaiting_remind_answer = False
            await q.edit_message_text(f"✅ Ок, запрос #{req_id} остаётся активным.")
            return

        if action == "drop":
            await delete_request_everywhere(context.application, req, reason="не актуально")
            try:
                await q.edit_message_text(f"🧹 Запрос #{req_id} удалён как неактуальный.")
            except Exception:
                pass
            return

    if data.startswith("reply_to_agent|"):
        # author presses "reply to agent" button to choose active chat
        _, req_id, agent_id_str = data.split("|", 2)
        agent_id = int(agent_id_str)
        req = REQUESTS.get(req_id)
        if not req:
            await q.edit_message_text("Запрос уже неактивен.")
            return
        if q.from_user.id != req.author_id:
            await q.edit_message_text("Эта кнопка только для автора запроса.")
            return

        set_active_chat(req.author_id, agent_id, req_id)
        await q.edit_message_text(
            f"✅ Активный чат выбран.\n"
            f"Теперь просто пиши сообщения в этот чат с ботом — они будут уходить агенту.\n"
            f"Сессия закроется через 1 час без активности."
        )
        return


# =========================
# PRIVATE TEXT HANDLER
# =========================
async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return

    text = (update.message.text or "").strip()

    # agent reply mode
    if context.user_data.get("mode") == "agent_reply":
        req_id = context.user_data.get("reply_req_id")
        if text.upper() == "ГОТОВО":
            context.user_data["mode"] = None
            context.user_data["reply_req_id"] = None
            await update.message.reply_text("Ок, режим ответа завершён.")
            return

        req = REQUESTS.get(req_id)
        if not req or req.status != "active":
            await update.message.reply_text("Этот запрос уже неактивен.")
            return

        agent = update.effective_user
        agent_name = normalize_username(agent.username or agent.first_name or "")
        agent_id = agent.id

        # send to author
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("💬 Ответить агенту", callback_data=f"reply_to_agent|{req_id}|{agent_id}")]]
        )
        await context.bot.send_message(
            chat_id=req.author_id,
            text=f"📩 Вариант по запросу #{req_id}\nОт: {agent_name}\n\n{text}",
            reply_markup=kb,
            disable_web_page_preview=False,
        )

        # make chat active automatically (so author can reply right away)
        set_active_chat(req.author_id, agent_id, req_id)
        set_active_chat(agent_id, req.author_id, req_id)

        await update.message.reply_text("✅ Отправлено автору запроса.")
        return

    # normal private chat forwarding (active chat)
    ac = get_active_chat(update.effective_user.id)
    if ac:
        # forward message to peer
        try:
            await context.bot.send_message(
                chat_id=ac.peer_id,
                text=f"💬 Сообщение по запросу #{ac.req_id}:\n\n{text}",
                disable_web_page_preview=True,
            )
            await update.message.reply_text("✅ Отправлено.")
        except Exception:
            await update.message.reply_text("Не смог отправить сообщение. Попробуй позже.")
        return

    # no active chat
    await update.message.reply_text(
        "Сообщение никуда не отправлено.\n\n"
        "Если хочешь ответить агенту — нажми кнопку «Ответить агенту» в его сообщении.\n"
        "Или открой /my и выбери нужный запрос.",
        disable_web_page_preview=True,
    )


# =========================
# MAINTENANCE
# =========================
async def maintenance_job(app: Application) -> None:
    # cleanup expired chats
    t = now_ts()
    expired_users = [uid for uid, ac in ACTIVE_CHATS.items() if ac.expires_at < t]
    for uid in expired_users:
        ACTIVE_CHATS.pop(uid, None)

    # reminders / auto delete
    for req in list(REQUESTS.values()):
        if req.status != "active":
            continue

        # if request already older than TTL - ask
        age = t - req.created_at
        due = t - req.last_remind_at >= REMIND_EVERY_SECONDS

        if age >= REQUEST_TTL_SECONDS and due and not req.awaiting_remind_answer:
            req.awaiting_remind_answer = True

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Да, актуально", callback_data=f"keep|{req.req_id}"),
                        InlineKeyboardButton("🧹 Нет, удалить", callback_data=f"drop|{req.req_id}"),
                    ]
                ]
            )
            try:
                await app.bot.send_message(
                    chat_id=req.author_id,
                    text=f"⏰ Запрос #{req.req_id} всё ещё актуален?\n"
                         f"Если не актуально — он будет удалён из канала.",
                    reply_markup=kb,
                )
            except Exception:
                # if can't contact author, just leave it
                req.awaiting_remind_answer = False


async def post_init(app: Application) -> None:
    # start repeating maintenance
    app.job_queue.run_repeating(lambda _: maintenance_job(app), interval=MAINTENANCE_INTERVAL_SECONDS, first=MAINTENANCE_INTERVAL_SECONDS)


# =========================
# MAIN
# =========================
def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("request", request_cmd)],
        states={
            ST_DISTRICTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_districts)],
            ST_ROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_rooms)],
            ST_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_budget)],
            ST_BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_bedrooms)],
            ST_PETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_pets)],
            ST_AMENITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_amenities)],
            ST_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_area)],
            ST_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_comment)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("my", my_cmd))

    application.add_handler(conv)
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_text))

    return application


def main() -> None:
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
