# main.py
# ─────────────────────────────────────────────────────────────────────────────
# Точка входа MasterBot 12.92-refactor
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import logging
import logging.handlers
from pathlib import Path
import json

from aiogram import Bot, Dispatcher
from aiogram.client.bot import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

from core.config import settings
from core.db import init_db
from core.state import state
from core.utils import delete_previous_private_messages
from handlers import setup as setup_handlers
from handlers.polls import get_main_menu

# ───────────────── Logging ──────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "masterbot.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ───────────────── Startup ─────────────────────────────────────────────────
async def on_startup() -> None:
    """
    1) Загружает settings в state.config, дополняет domain и svetofor_spreadsheet_id.
    2) Загружает admin_chat_id из chat_id.json.
    3) Инициализирует SQLite (await init_db()).
    """
    # 1) state.config
    if hasattr(settings, "model_dump"):
        state.config = settings.model_dump()
    else:
        state.config = settings.dict()
    state.config["domain"] = settings.AMO_DOMAIN
    state.svetofor_spreadsheet_id = settings.SVETOFOR_SPREAD_ID

    # 2) admin_chat_id
    chat_file = Path(__file__).parent / "chat_id.json"
    if chat_file.exists():
        try:
            data = json.loads(chat_file.read_text(encoding="utf-8"))
            state.admin_chat_id = data.get("admin_chat_id")
            logger.info("[startup] admin_chat_id загружен: %s", state.admin_chat_id)
        except Exception as e:
            logger.error("[startup] Не удалось загрузить admin_chat_id: %s", e, exc_info=True)
    else:
        logger.warning("[startup] Файл chat_id.json не найден, admin_chat_id не установлен")

    # 3) БД
    await init_db()
    logger.info(
        "[startup] Конфиг и БД инициализированы, Leader-ID %d гарантирован",
        settings.LEADER_ID
    )

# ────────────────── /start handler ────────────────────────────────────────
async def start_handler(message: Message) -> None:
    """
    Приветствие и показ главного меню при /start.
    """
    uid = message.from_user.id
    try:
        await delete_previous_private_messages(uid)
        kb = await get_main_menu(uid)
        greet = (
            f"🎉 Привет, {message.from_user.first_name or 'Пользователь'}!\n"
            f"Я *MasterBot* {settings.VERSION} 🤖\n"
            f"Твой ID: `{uid}`\n"
        )
        if not kb:
            greet += "Роли ещё не назначены, поэтому меню скрыто."
        else:
            greet += "Я помогу с квестами и распределением ведущих."
        await message.answer(greet, reply_markup=kb)
    except Exception as e:
        logger.error("[start_handler] Failed for user %d: %s", uid, e, exc_info=True)
        await message.answer("⚠️ Произошла ошибка, попробуйте позже.")

# ────────────────── main ──────────────────────────────────────────────────
async def main() -> None:
    # 0) создаём экземпляр бота
    bot = Bot(
        token=settings.API_TOKEN,
        default=DefaultBotProperties(parse_mode="Markdown"),
    )

    # Патчим Bot.get_current(), чтобы утилиты могли достать объект бота
    Bot.get_current = classmethod(lambda cls: bot)

    dp = Dispatcher()

    # Регистрируем startup и /start
    dp.startup.register(on_startup)
    dp.message.register(start_handler, CommandStart())

    # Подключаем все обработчики
    setup_handlers(dp)

    # Запускаем планировщик в московском часовом поясе
    scheduler = AsyncIOScheduler(timezone=timezone("Europe/Moscow"))
    scheduler.start()

    logger.info("Bot starting… version %s", settings.VERSION)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
