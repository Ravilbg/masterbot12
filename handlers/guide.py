# handlers/guide.py — «бот-проводник» для группового чата ведущих
# ────────────────────────────────────────────────────────────────────
"""
MasterBot v14.7 · 2025-08-08

Fix 14.7
• custom_button_handler теперь работает **только** в группах / супергруппах,
  поэтому не перехватывает личные сообщения («🎲 Мои игры» и т.п.).
• Исключение SkipHandler больше не поднимается — если кнопка не совпала,
  хендлер просто возвращает управление без ошибок.
• Остальной функционал (пин-меню, SQLite, логика кастом-кнопок) неизменён.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import List, Tuple

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
state.group_menu_message_id = getattr(state, "group_menu_message_id", {})

# ███ [1] КОНСТАНТЫ
# --------------------------------------------------------------------
PROFILE_BUTTON_TEXT = "👤 Личный кабинет"
PROFILE_LINK = "https://t.me/masbot12_bot?start=profile"  # TODO: real link

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
    logger.debug("[guide] fetched %d custom buttons for chat %d", len(rows), chat_id)
    return rows


# ███ [3] Меню и пиннинг
# --------------------------------------------------------------------
def build_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=PROFILE_BUTTON_TEXT, url=PROFILE_LINK)]]
    )


async def ensure_pinned_menu(chat_id: int) -> None:
    bot = Bot.get_current()
    menu_id = state.group_menu_message_id.get(chat_id)
    markup = build_menu_markup()

    if menu_id:
        try:
            await bot.edit_message_reply_markup(chat_id, menu_id, reply_markup=markup)
            logger.debug("[guide] updated pinned menu %d in chat %d", menu_id, chat_id)
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
    with contextlib.suppress(Exception):
        await UnpinAllChatMessages(chat_id=chat_id)
    await PinChatMessage(chat_id=chat_id, message_id=sent.message_id)
    state.group_menu_message_id[chat_id] = sent.message_id
    logger.info("[guide] pinned new menu %d in chat %d", sent.message_id, chat_id)


# ███ [4] Групповое меню для main.py
# --------------------------------------------------------------------
def group_keyboard() -> InlineKeyboardMarkup:
    return build_menu_markup()


# ███ [5] ОБРАБОТЧИКИ
# --------------------------------------------------------------------
@router.my_chat_member()
async def on_bot_join(evt: ChatMemberUpdated) -> None:
    if evt.new_chat_member.status in {"member", "administrator"}:
        logger.info(
            "[guide] bot joined chat %d as %s", evt.chat.id, evt.new_chat_member.status
        )
        await ensure_pinned_menu(evt.chat.id)


@router.message(CommandStart())
async def on_group_start(message: Message) -> None:
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        logger.info("[guide] /start in group %d", message.chat.id)
        await ensure_pinned_menu(message.chat.id)


@router.message(lambda m: m.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP})
async def custom_button_handler(message: Message) -> None:
    """Обработка кастом-кнопок *только* в группах.  
    В личке — сразу возвращаем управление другим хендлерам.
    """
    txt = (message.text or "").strip()
    if not txt:
        return

    for text, url in fetch_custom_buttons(message.chat.id):
        if txt == text:
            logger.info("[guide] custom %r clicked in chat %d", text, message.chat.id)
            await message.reply(
                "\u200B",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]]
                ),
                disable_web_page_preview=False,
            )
            return

    logger.debug("[guide] no custom match for %r in chat %d", txt, message.chat.id)
    # просто выходим — другие хендлеры (личного кабинета и др.) обработают сообщение


# ███ [6] ИНИЦИАЛИЗАЦИЯ
# --------------------------------------------------------------------
init_db()
logger.info("[guide] module loaded, DB=%s", DB_FILE)
