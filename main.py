"""main.py — точка входа MasterBot 15.4
────────────────────────────────────────────────────────────────────────────
⚙️  Новое в 15.4
• LOG_LEVEL по-умолчанию → DEBUG (видно всё, даже без переменной окружения).
• Middleware UpdateLogger — пишет тип и короткий дайджест КАЖДОГО апдейта.
• При старте печатаем путь импортированного handlers.my_games/profile —
  полезно, если в проекте остались «дубликаты».
• Остальной функционал НЕ изменён.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message, TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

from core.config import settings
from core.db import init_db
from core.menu import get_main_menu
from core.state import state
from core.utils import delete_previous_private_messages
from handlers import setup as setup_handlers
from handlers.guide import group_keyboard, router as guide_router
from handlers.profile import profile_handler

# side-routers (импортами, чтобы не потерять F401)
from handlers.confirmations import router as _r1  # noqa: F401
from handlers.stats          import router as _r2  # noqa: F401
from handlers.profile        import router as _r3  # noqa: F401
from handlers.my_games       import router as _r4  # noqa: F401
from handlers.bonuses        import router as _r5  # noqa: F401

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# [1] LOGGING
# ════════════════════════════════════════════════════════════════════
LOG_DIR  = Path(settings.LOG_DIR)
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "masterbot.log"


def _setup_logging() -> None:
    """Инициализация logging с ротацией и уровнем из $LOG_LEVEL (DEBUG по умолч.)."""
    level_name = os.getenv("LOG_LEVEL", "DEBUG").upper()
    root_level = getattr(logging, level_name, logging.DEBUG)

    fmt     = "%(asctime)s [%(levelname).1s] %(name)s:%(lineno)d — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=7, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ]

    logging.basicConfig(
        level=root_level,
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )

    # наши пакеты — INFO/DEBUG
    for pkg in ("core", "handlers", "services"):
        logging.getLogger(pkg).setLevel(
            logging.DEBUG if root_level == logging.DEBUG else logging.INFO
        )
    # сторонние библиотеки — WARNING
    for noisy in ("aiosqlite", "googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


_setup_logging()

# печатаем, откуда реально грузятся файлы с кнопкой «Мои игры»
import handlers.my_games as _mg
import handlers.profile as _pf

logger.debug("✅ using my_games from %s", _mg.__file__)
logger.debug("✅ using profile  from %s", _pf.__file__)

# ════════════════════════════════════════════════════════════════════
# [1.1] MIDDLEWARE: ЛОГ КАЖДОГО АПДЕЙТА
# ════════════════════════════════════════════════════════════════════
class UpdateLogger(BaseMiddleware):
    """Пишет INFO о любом пришедшем апдейте (тип и короткий дайджест)."""

    async def __call__(
        self,
        handler: Any,
        event: TelegramObject,
        data: dict,
    ):
        t = type(event).__name__
        # text, data или просто repr(event)
        short = getattr(event, "text", "") or getattr(event, "data", "") or repr(event)
        short = (short[:60] + "…") if len(short) > 60 else short
        logger.info("[update] %-15s %s", t, short)
        return await handler(event, data)


# ════════════════════════════════════════════════════════════════════
# [2] STARTUP
# ════════════════════════════════════════════════════════════════════
async def on_startup() -> None:
    state.config = (
        settings.model_dump() if hasattr(settings, "model_dump") else settings.dict()
    )
    state.config["domain"] = settings.AMO_DOMAIN
    state.svetofor_spreadsheet_id = settings.SVETOFOR_SPREAD_ID

    chat_file = Path(__file__).parent / "chat_id.json"
    if chat_file.exists():
        with contextlib.suppress(Exception):
            data = json.loads(chat_file.read_text("utf-8"))
            state.admin_chat_id = data.get("admin_chat_id")
    logger.info("[startup] admin_chat_id=%s", state.admin_chat_id)

    await init_db()
    logger.info("[startup] DB initialized, Leader-ID=%d", settings.LEADER_ID)

# ════════════════════════════════════════════════════════════════════
# [3] /start HANDLERS
# ════════════════════════════════════════════════════════════════════
async def _send_main_menu(uid: int) -> None:
    await delete_previous_private_messages(uid)
    kb = await get_main_menu(uid)
    bot = Bot.get_current()
    if kb:
        await bot.send_message(uid, "\u2060", reply_markup=kb)
    else:
        await bot.send_message(uid, "⛔ У вас пока нет доступа к функциям бота.")


async def group_start(message: Message) -> None:
    await message.answer(
        "📌 *Откройте личный кабинет для своих игр:*",
        reply_markup=group_keyboard(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def private_start(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await _send_main_menu(message.from_user.id)


async def legacy_profile_start(message: Message) -> None:
    if (message.text or "").strip().lower() == "/start profile":
        await delete_previous_private_messages(message.from_user.id)
        await profile_handler(message)
        await _send_main_menu(message.from_user.id)

# ════════════════════════════════════════════════════════════════════
# [4] MAIN
# ════════════════════════════════════════════════════════════════════
async def main() -> None:
    bot = Bot(token=settings.API_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
    Bot.get_current = classmethod(lambda cls: bot)  # type: ignore

    dp = Dispatcher()
    dp.startup.register(on_startup)

    dp.message.middleware(UpdateLogger())
    dp.callback_query.middleware(UpdateLogger())

    dp.message.register(
        group_start,
        CommandStart(),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    )
    dp.message.register(
        private_start,
        CommandStart(),
        F.chat.type == ChatType.PRIVATE,
    )
    dp.message.register(legacy_profile_start, CommandStart())

    setup_handlers(dp)
    dp.include_router(guide_router)
    logger.info("[setup] routers registered: %d", len(getattr(dp, 'sub_routers', [])))

    scheduler = AsyncIOScheduler(timezone=timezone("Europe/Moscow"))
    scheduler.start()

    logger.info("🤖 Bot starting, version=%s", settings.VERSION)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        scheduler.shutdown()
        logger.info("Bot stopped")

# ════════════════════════════════════════════════════════════════════
# [99] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    from core.db import get_user_info as _orig_get_user_info

    async def _fake(_uid: int) -> dict:
        return {"role": settings.ACCESS["poll"][0]}

    import core.db as _db
    _db.get_user_info = _fake  # type: ignore
    kb = await get_main_menu(1)
    assert kb and len(kb.keyboard) > 0, "Главное меню не сгенерировалось"
    _db.get_user_info = _orig_get_user_info  # type: ignore
    print("main.py smoke-test OK")


# ════════════════════════════════════════════════════════════════════
# [∞] ENTRYPOINT
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
    except Exception:
        logger.exception("Fatal error")
