# handlers/guide.py — «бот-проводник» для группового чата ведущих
# ────────────────────────────────────────────────────────────────────
"""
MasterBot v14.3 · 2025-08-06

В группе — закреплённое сообщение с inline-кнопкой:
  • 👤 Личный кабинет → PROFILE_LINK

• Меню auto-pinned при /start в группе или при добавлении бота.
• Поддержка кастомных inline-кнопок из SQLite по тексту сообщения.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.methods import PinChatMessage, UnpinAllChatMessages
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.state import state

logger = logging.getLogger(__name__)
router = Router()

# ensure storage for pinned menus
if not hasattr(state, "group_menu_message_id"):
    state.group_menu_message_id = {}  # chat_id → pinned message_id

# ███ [1] КОНСТАНТЫ
# --------------------------------------------------------------------
PROFILE_BUTTON_TEXT = "👤 Личный кабинет"

# TODO: заменить на реальную ссылку
PROFILE_LINK = "https://t.me/masbot12_bot?start=profile"

# ███ [2] SQLite — кастомные кнопки
# --------------------------------------------------------------------
DB_FILE = Path(__file__).resolve().parent / "checklists.db"
_conn = sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db() -> None:
    with _conn:
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_buttons (
                chat_id     INTEGER,
                button_text TEXT,
                button_url  TEXT,
                PRIMARY KEY(chat_id, button_text)
            )
        """
        )
    logger.debug("[guide] custom_buttons table initialized at %s", DB_FILE)


def fetch_custom_buttons(chat_id: int) -> List[Tuple[str, str]]:
    cur = _conn.execute(
        "SELECT button_text, button_url FROM custom_buttons WHERE chat_id = ?",
        (chat_id,),
    )
    rows = cur.fetchall()
    logger.debug(
        "[guide] fetched %d custom buttons for chat %d", len(rows), chat_id
    )
    return rows


# ███ [3] Меню и пиннинг
# --------------------------------------------------------------------
def build_menu_markup() -> InlineKeyboardMarkup:
    """Inline-клавиатура для закреплённого меню в группе (одна кнопка)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=PROFILE_BUTTON_TEXT, url=PROFILE_LINK)]
        ]
    )


async def ensure_pinned_menu(chat_id: int) -> None:
    """
    Создаёт или обновляет закреплённое меню в группе chat_id.
    """
    bot = Bot.get_current()
    menu_id = state.group_menu_message_id.get(chat_id)
    markup = build_menu_markup()

    if menu_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id, menu_id, reply_markup=markup
            )
            logger.debug(
                "[guide] updated pinned menu %d in chat %d", menu_id, chat_id
            )
            return
        except Exception as e:
            logger.warning("[guide] failed to update menu %d: %s", menu_id, e)

    sent = await bot.send_message(
        chat_id,
        "📌 *Личный кабинет ведущего:*",
        parse_mode="Markdown",
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    try:
        await UnpinAllChatMessages(chat_id=chat_id)
    except Exception:
        pass
    await PinChatMessage(chat_id=chat_id, message_id=sent.message_id)
    state.group_menu_message_id[chat_id] = sent.message_id
    logger.info("[guide] pinned new menu %d in chat %d", sent.message_id, chat_id)


# ███ [4] Групповое меню для main.py
# --------------------------------------------------------------------
def group_keyboard() -> InlineKeyboardMarkup:
    """
    Возвращает markup для reply_markup в main.py при /start в группе.
    """
    return build_menu_markup()


# ███ [5] Обработчики
# --------------------------------------------------------------------
@router.my_chat_member()
async def on_bot_join(evt: ChatMemberUpdated) -> None:
    """
    При добавлении бота в группу: обновляем/пинним меню.
    """
    if evt.new_chat_member.status in {"member", "administrator"}:
        logger.info(
            "[guide] bot joined chat %d as %s",
            evt.chat.id,
            evt.new_chat_member.status,
        )
        await ensure_pinned_menu(evt.chat.id)


@router.message(CommandStart())
async def on_group_start(message: Message) -> None:
    """
    При /start в групповом чате — создаём или обновляем меню.
    """
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        logger.info("[guide] /start in group %d", message.chat.id)
        await ensure_pinned_menu(message.chat.id)


@router.message()
async def custom_button_handler(message: Message) -> None:
    """
    Реакция на текст сообщения: если совпадает с кастомной кнопкой, шлём её inline.
    """
    txt = (message.text or "").strip()
    if not txt:
        return
    for text, url in fetch_custom_buttons(message.chat.id):
        if txt == text:
            logger.info(
                "[guide] custom %r clicked in chat %d", text, message.chat.id
            )
            await message.reply(
                "\u200B",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]]
                ),
                disable_web_page_preview=False,
            )
            return
    logger.debug(
        "[guide] no custom match for %r in chat %d", txt, message.chat.id
    )


# ███ [6] Инициализация
# --------------------------------------------------------------------
init_db()
logger.info("[guide] module loaded, DB=%s", DB_FILE)

# История изменений:
#   • v14.3 (2025-08-06) — оставлена только кнопка «👤 Личный кабинет» в закреплённом меню
