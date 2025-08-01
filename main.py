# main.py — точка входа MasterBot 12.97-cycle
# ───────────────────────────────────────────────────────────────────────────────
"""main.py — точка входа MasterBot 12.97-cycle
──────────────────────────────────────────────────────────────────────────────
Добавлено более подробное логирование:
• aiogram-framework переведён на INFO, чтобы видеть обработку апдейтов.
• Шумные библиотеки оставлены на WARNING.
• Логи не превращаются в простыню благодаря TrimFilter.
"""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

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
from handlers.polls_lifecycle import get_main_menu  # /start helper

# импортируем роутеры (не регистрируем повторно)
from handlers.confirmations import router as _confirmations_router  # noqa: F401
from handlers.stats import router as _stats_router  # noqa: F401

# ███ [2.0] LOGGING CONFIGURATION
# --------------------------------------------------------------------
LOG_DIR = Path(settings.LOG_DIR)
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "masterbot.log"

def _setup_logging() -> None:
    """Конфигурация логирования с подробностями, без лишнего шума."""
    # корневой уровень из окружения
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    root_level = getattr(logging, level_name, logging.INFO)

    # формат: время, уровень, модуль:строка, сообщение
    fmt = "%(asctime)s [%(levelname).1s] [%(name)s:%(lineno)d] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=7, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=root_level, format=fmt, datefmt=datefmt, handlers=handlers)

    # собстvenные пакеты: DEBUG если root DEBUG, иначе INFO
    own_pkgs = ("core", "handlers", "services")
    for pkg in own_pkgs:
        lvl = logging.DEBUG if root_level == logging.DEBUG else logging.INFO
        logging.getLogger(pkg).setLevel(lvl)

    # шумные библиотеки: WARNING
    noisy = ("aiosqlite", "googleapiclient", "urllib3")
    for pkg in noisy:
        logging.getLogger(pkg).setLevel(logging.WARNING)

    # включаем INFO для aiogram, чтобы видеть логи диспетчера и хендлеров
    logging.getLogger("aiogram").setLevel(logging.INFO)

    # фильтр обрезки слишком длинных сообщений
    class _TrimFilter(logging.Filter):
        __slots__ = ("_limit",)

        def __init__(self, limit: int = 350):
            super().__init__()
            self._limit = limit

        def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
            msg = record.getMessage()
            if len(msg) > self._limit:
                record.msg = msg[: self._limit - 1] + "…"
                record.args = ()
            return True

    logging.getLogger().addFilter(_TrimFilter())

# немедленная настройка логирования
_setup_logging()
logger = logging.getLogger(__name__)

# ███ [3.0] STARTUP HOOK
# --------------------------------------------------------------------
async def on_startup() -> None:
    """Инициализация состояния, БД и admin_chat_id."""
    # состояние конфигурации
    state.config = settings.model_dump() if hasattr(settings, "model_dump") else settings.dict()
    state.config["domain"] = settings.AMO_DOMAIN
    state.svetofor_spreadsheet_id = settings.SVETOFOR_SPREAD_ID
    logger.debug("[startup] state.config loaded: %r", state.config)

    # admin_chat_id из chat_id.json
    chat_file = Path(__file__).parent / "chat_id.json"
    if chat_file.exists():
        try:
            data = json.loads(chat_file.read_text(encoding="utf-8"))
            state.admin_chat_id = data.get("admin_chat_id")
            logger.info("[startup] admin_chat_id: %s", state.admin_chat_id)
        except Exception as exc:
            logger.exception("[startup] Ошибка чтения chat_id.json: %s", exc)
    else:
        logger.warning("[startup] chat_id.json не найден, admin_chat_id не задан")

    # инициализация БД
    try:
        await init_db()
        logger.info("[startup] DB initialized, Leader-ID %d", settings.LEADER_ID)
    except Exception as exc:
        logger.exception("[startup] init_db failed: %s", exc)
        raise

# ███ [4.0] /start HANDLER
# --------------------------------------------------------------------
async def _start_handler(message: Message) -> None:
    """/start: приветствие + меню."""
    uid = message.from_user.id
    logger.debug("[/start] Received /start from %d (%r)", uid, message.from_user.first_name)
    try:
        await delete_previous_private_messages(uid)
        kb = await get_main_menu(uid)
        greet = (
            f"🎉 Привет, {message.from_user.first_name or 'Пользователь'}!\n"
            f"Я *MasterBot* {settings.VERSION} 🤖\n"
            f"Твой ID: `{uid}`\n"
        )
        greet += (
            "Я помогу с квестами и распределением ведущих." if kb
            else "Роли ещё не назначены, меню скрыто."
        )
        await message.answer(greet, reply_markup=kb)
        logger.info("[/start] Greet sent to %d, menu shown=%s", uid, bool(kb))
    except Exception as exc:
        logger.exception("[/start] Error handling /start: %s", exc)
        await message.answer("⚠️ Произошла ошибка, попробуйте позже.")

# ███ [5.0] MAIN–LOOP
# --------------------------------------------------------------------
async def main() -> None:
    """Запускает бота и планировщик."""
    bot = Bot(
        token=settings.API_TOKEN,
        default=DefaultBotProperties(parse_mode="Markdown"),
    )
    Bot.get_current = classmethod(lambda cls: bot)  # type: ignore
    dp = Dispatcher()

    dp.startup.register(on_startup)
    dp.message.register(_start_handler, CommandStart())
    setup_handlers(dp)

    scheduler = AsyncIOScheduler(timezone=timezone("Europe/Moscow"))
    scheduler.start()
    logger.info("Bot starting, version=%s", settings.VERSION)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        scheduler.shutdown()
        logger.info("Bot stopped")

# ███ [6.0] _TEST
# --------------------------------------------------------------------
async def _test() -> None:
    """Тест основных компонентов main.py."""
    logger.info("[test] Logging OK, level=%s", logging.getLevelName(logging.getLogger().level))
    state.config.clear()
    await on_startup()
    assert state.config, "state.config is empty"
    assert state.admin_chat_id is None or isinstance(state.admin_chat_id, int)
    print("main.py tests passed")

# ███ [7.0] ENTRYPOINT
# --------------------------------------------------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
    except Exception as exc:
        logger.exception("Unhandled exception: %s", exc)
