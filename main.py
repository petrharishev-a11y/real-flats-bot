import os
import time
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # НЕ вставляй токен в код
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").lstrip("@")  # например: Real_Flat_Bot
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID")  # например: -5049595468

GROUP_CHAT_ID: Optional[int] = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else None

REQUEST_TTL_SECONDS = int(os.getenv("REQUEST_TTL_SECONDS", "172800"))  # 48 часов
WATCH_INTERVAL_SECONDS = int(os.getenv("WATCH_INTERVAL_SECONDS", "600"))  # 10 минут

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required (set it in Render Environment Variables)")

# =========================
# DATA
# =========================
@dataclass
class Request:
    rid: int
    author_id: int
    author_username: str
    created_at: float
    status: str = "active"  # active/closed
    area: str = ""
    budget: str = ""
    rooms: str = ""
    urgency: str = ""
    pets: str = ""
    taken_by_id: Optional[int] = None
    taken_by_username: Optional[str] = None
    group_message_id: Optional[int] = None
    last_ttl_prompt_at: float = 0.0


REQUESTS: Dict[int, Request] = {}
GROUP_MSG_TO_RID: Dict[int, int] = {}
NEXT_RID = 1

# Conversation states
AREA, BUDGET, ROOMS, URGENCY, PETS, CONFIRM = range(6)

# Callback prefixes
CB_TAKE = "TAKE"
CB_CLOSE = "CLOSE"
CB_TTL_YES = "TTLYES"
CB_TTL_NO = "TTLNO"
CB_CONFIRM = "CONFIRM"
CB_CANCEL = "CANCEL"


# =========================
# HELPERS
# =========================
def _user_tag(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "unknown"
    return f"@{u.username}" if u.username else (u.first_name or "user")


def _next_rid() -> int:
    global NEXT_RID
    rid = NEXT_RID
    NEXT_RID += 1
    return rid


def _request_text(r: Request) -> str:
    taken = ""
    if r.taken_by_username:
        taken = f"\n👤 Взял: @{r.taken_by_username}"
    elif r.taken_by_id:
        taken = f"\n👤 Взял: {r.taken_by_id}"

    return (
        f"📌 Запрос #{r.rid}\n"
        f"От: {r.author_username} (id {r.author_id})\n\n"
        f"Районы: {r.area}\n"
        f"Бюджет: {r.budget}\n"
        f"Комнаты/спальни: {r.rooms}\n"
        f"Срочность: {r.urgency}\n"
        f"Животные: {r.pets}\n"
        f"Статус: {r.status}"
        f"{taken}\n\n"
        f"➡️ Ответьте на это сообщение ссылками/вариантами — бот отправит их клиенту."
    )


def _group_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Взять", callback_data=f"{CB_TAKE}:{rid}"),
                InlineKeyboardButton("🛑 Закрыть", callback_data=f"{CB_CLOSE}:{rid}"),
            ]
        ]
    )


def _ttl_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да, актуально", callback_data=f"{CB_TTL_YES}:{rid}"),
                InlineKeyboardButton("🛑 Нет, закрыть", callback_data=f"{CB_TTL_NO}:{rid}"),
            ]
        ]
    )


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот Real Flats.\n\n"
        "Создать запрос: /request\n"
        "Мои активные запросы: /my\n"
        "Помощь: /help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/request — создать запрос\n"
        "/my — посмотреть свои активные запросы\n"
        "/close <id> — закрыть запрос\n"
        "/ping — проверка"
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong ✅")


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    active = [r for r in REQUESTS.values() if r.author_id == uid and r.status == "active"]
    if not active:
        await update.message.reply_text("У тебя нет активных запросов.")
        return

    lines = ["Твои активные запросы:"]
    for r in sorted(active, key=lambda x: x.rid):
        lines.append(f"• #{r.rid} — {r.area} | {r.budget} | {r.rooms}")
    await update.message.reply_text("\n".join(lines))


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Напиши так: /close 12")
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Пример: /close 12")
        return

    r = REQUESTS.get(rid)
    if not r:
        await update.message.reply_text("Такого запроса нет.")
        return
    if r.author_id != update.effective_user.id:
        await update.message.reply_text("Ты не автор этого запроса.")
        return

    r.status = "closed"
    await update.message.reply_text(f"Запрос #{rid} закрыт ✅")


# =========================
# REQUEST CONVERSATION
# =========================
async def request_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["req"] = {}
    await update.message.reply_text("Ок, начнём.\n\n1) Какие районы? (можно несколько)")
    return AREA


async def request_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["req"]["area"] = (update.message.text or "").strip()
    await update.message.reply_text("2) Бюджет? (например: $800–1200)")
    return BUDGET


async def request_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["req"]["budget"] = (update.message.text or "").strip()
    await update.message.reply_text("3) Комнаты/спальни? (например: 2к / 1 спальня)")
    return ROOMS


async def request_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["req"]["rooms"] = (update.message.text or "").strip()
    await update.message.reply_text("4) Срочность? (когда нужно заехать / когда заканчивается договор)")
    return URGENCY


async def request_urgency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["req"]["urgency"] = (update.message.text or "").strip()
    await update.message.reply_text("5) Животные? (нет / да, кто?)")
    return PETS


async def request_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["req"]["pets"] = (update.message.text or "").strip()

    data = context.user_data.get("req", {})
    preview = (
        "Проверь, всё ок?\n\n"
        f"Районы: {data.get('area','')}\n"
        f"Бюджет: {data.get('budget','')}\n"
        f"Комнаты/спальни: {data.get('rooms','')}\n"
        f"Срочность: {data.get('urgency','')}\n"
        f"Животные: {data.get('pets','')}\n"
    )
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"{CB_CONFIRM}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"{CB_CANCEL}"),
        ]]
    )
    await update.message.reply_text(preview, reply_markup=kb)
    return CONFIRM


async def request_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CB_CANCEL:
        await query.edit_message_text("Ок, отменил. Если нужно — /request")
        return ConversationHandler.END

    data = context.user_data.get("req", {})
    rid = _next_rid()
    u = update.effective_user
    author_username = f"@{u.username}" if u and u.username else (u.first_name if u else "user")

    r = Request(
        rid=rid,
        author_id=u.id,
        author_username=author_username,
        created_at=time.time(),
        area=data.get("area", ""),
        budget=data.get("budget", ""),
        rooms=data.get("rooms", ""),
        urgency=data.get("urgency", ""),
        pets=data.get("pets", ""),
    )
    REQUESTS[rid] = r

    # Сообщение клиенту
    await query.edit_message_text(f"Готово ✅ Запрос #{rid} создан. Жду варианты от агентов.")

    # Пост в группу агентов
    if GROUP_CHAT_ID:
        try:
            msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=_request_text(r),
                reply_markup=_group_keyboard(rid),
            )
            r.group_message_id = msg.message_id
            GROUP_MSG_TO_RID[msg.message_id] = rid
        except Exception as e:
            # Не падаем, просто говорим клиенту
            await context.bot.send_message(
                chat_id=r.author_id,
                text="⚠️ Не смог отправить запрос в группу агентов (проверь, что бот добавлен в группу и у него есть права)."
            )

    else:
        await context.bot.send_message(
            chat_id=r.author_id,
            text="⚠️ GROUP_CHAT_ID не задан — я создал запрос, но не могу отправить его агентам. Добавь GROUP_CHAT_ID в Render."
        )

    return ConversationHandler.END


async def request_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменил. Если нужно — /request")
    return ConversationHandler.END


# =========================
# GROUP HANDLING (агенты)
# =========================
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if GROUP_CHAT_ID is None or update.effective_chat.id != GROUP_CHAT_ID:
        return

    # Нужно, чтобы агент отвечал реплаем на сообщение запроса
    if not update.message.reply_to_message:
        return

    parent_id = update.message.reply_to_message.message_id
    rid = GROUP_MSG_TO_RID.get(parent_id)
    if not rid:
        # иногда редактируют/перепостят — попробуем вытащить по тексту
        text = update.message.reply_to_message.text or ""
        # "Запрос #12"
        import re
        m = re.search(r"#(\d+)", text)
        if m:
            rid = int(m.group(1))
        else:
            return

    r = REQUESTS.get(rid)
    if not r or r.status != "active":
        await update.message.reply_text("Этот запрос уже закрыт/не найден.")
        return

    agent = update.effective_user
    agent_tag = f"@{agent.username}" if agent and agent.username else (agent.first_name if agent else "agent")

    try:
        # Копируем сообщение агентa клиенту (без ссылки на группу)
        await context.bot.copy_message(
            chat_id=r.author_id,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
        await update.message.reply_text(f"✅ Отправлено клиенту по запросу #{rid} ({agent_tag}). Можешь слать ещё.")
    except Exception:
        await update.message.reply_text("⚠️ Не смог отправить клиенту (возможно, он не нажал /start у бота).")


# =========================
# CALLBACKS (кнопки)
# =========================
async def callbacks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    # CONFIRM/CANCEL обрабатывается ConversationHandler'ом
    if data in (CB_CONFIRM, CB_CANCEL):
        return

    if ":" not in data:
        return
    prefix, rid_s = data.split(":", 1)
    try:
        rid = int(rid_s)
    except ValueError:
        return

    r = REQUESTS.get(rid)
    if not r:
        await query.edit_message_text("Запрос не найден.")
        return

    # Взять / закрыть в группе
    if prefix == CB_TAKE:
        if r.status != "active":
            await query.edit_message_text("Запрос уже закрыт.")
            return
        u = query.from_user
        r.taken_by_id = u.id
        r.taken_by_username = u.username or u.first_name
        # обновим сообщение в группе
        try:
            await query.edit_message_text(_request_text(r), reply_markup=_group_keyboard(rid))
        except Exception:
            pass
        # уведомим клиента
        try:
            await context.bot.send_message(
                chat_id=r.author_id,
                text=f"✅ Запрос #{rid} взял агент @{r.taken_by_username}. Скоро будут варианты.",
            )
        except Exception:
            pass
        return

    if prefix == CB_CLOSE:
        r.status = "closed"
        try:
            await query.edit_message_text(_request_text(r))
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=r.author_id, text=f"🛑 Запрос #{rid} закрыт.")
        except Exception:
            pass
        return

    # TTL buttons в личке клиента
    if prefix == CB_TTL_YES:
        if r.status != "active":
            await query.edit_message_text("Этот запрос уже закрыт.")
            return
        # продлеваем: просто «обновим» created_at, чтобы отсчёт пошёл заново
        r.created_at = time.time()
        r.last_ttl_prompt_at = time.time()
        await query.edit_message_text(f"Ок ✅ Продлил запрос #{rid} ещё на 48 часов.")
        return

    if prefix == CB_TTL_NO:
        r.status = "closed"
        await query.edit_message_text(f"Ок ✅ Закрыл запрос #{rid}.")
        return


# =========================
# TTL WATCHER
# =========================
async def ttl_watcher(application: Application) -> None:
    while True:
        now = time.time()
        for r in list(REQUESTS.values()):
            if r.status != "active":
                continue

            age = now - r.created_at
            if age < REQUEST_TTL_SECONDS:
                continue

            # чтобы не спамить часто
            if now - (r.last_ttl_prompt_at or 0) < 12 * 3600:
                continue

            try:
                await application.bot.send_message(
                    chat_id=r.author_id,
                    text=(
                        f"⏰ Запрос #{r.rid} живёт уже 48 часов.\n"
                        f"Актуально? (нажми кнопку ниже)"
                    ),
                    reply_markup=_ttl_keyboard(r.rid),
                )
                r.last_ttl_prompt_at = now
            except Exception:
                # пользователь мог не иметь диалога с ботом
                pass

        await asyncio.sleep(WATCH_INTERVAL_SECONDS)


async def post_init(application: Application) -> None:
    # ВАЖНО: используем application.create_task (а не asyncio.create_task)
    application.create_task(ttl_watcher(application))


# =========================
# MAIN
# =========================
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("request", request_entry)],
        states={
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_area)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_budget)],
            ROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_rooms)],
            URGENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_urgency)],
            PETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_pets)],
            CONFIRM: [CallbackQueryHandler(request_confirm_callback, pattern=f"^({CB_CONFIRM}|{CB_CANCEL})$")],
        },
        fallbacks=[CommandHandler("cancel", request_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("my", my_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(conv)

    # Кнопки TAKE/CLOSE/TTLYES/TTLNO
    app.add_handler(CallbackQueryHandler(callbacks_handler))

    # Сообщения агентов в группе
    app.add_handler(MessageHandler(filters.Chat(GROUP_CHAT_ID) & filters.ALL, group_message_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
