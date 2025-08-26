# handlers/guide.py — «бот-проводник» для группового чата ведущих
# ────────────────────────────────────────────────────────────────────
"""
MasterBot v14.8 · 2025-08-24

Fix 14.8
• Переход на методы экземпляра Bot для пинов/распинов (aiogram 3.x):
  bot.pin_chat_message(...) / bot.unpin_all_chat_messages(...)
  вместо прямых вызовов классов методов (PinChatMessage / UnpinAllChatMessages).
• Поведение без изменений: авто-пин меню в группе, обновление разметки,
  обработка кастом-кнопок только в группах, SQLite-хранилище.
• Добавлены типы и защитные проверки для state.group_menu_message_id.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.state import state

logger = logging.getLogger(__name__)
router = Router()

# ensure storage for pinned menus (chat_id -> message_id)
if not hasattr(state, "group_menu_message_id") or not isinstance(getattr(state, "group_menu_message_id"), dict):
    state.group_menu_message_id = {}  # type: ignore[attr-defined]
group_menu_message_id: Dict[int, int] = state.group_menu_message_id  # alias с типом

# ███ [1] КОНСТАНТЫ
# --------------------------------------------------------------------
PROFILE_BUTTON_TEXT = "👤 Личный кабинет"
PROFILE_LINK = "https://t.me/masbot12_bot?start=profile"  # TODO: real link

# ███ [2] SQLite — кастомные кнопки
# --------------------------------------------------------------------
DB_FILE = Path(__file__).resolve().parent / "checklists.db"
_conn: sqlite3.Connection = sqlite3.connect(DB_FILE, check_same_thread=False)


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
    rows: List[Tuple[str, str]] = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    logger.debug("[guide] fetched %d custom buttons for chat %d", len(rows), chat_id)
    return rows


# ███ [3] Меню и пиннинг
# --------------------------------------------------------------------
def build_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=PROFILE_BUTTON_TEXT, url=PROFILE_LINK)]]
    )


async def ensure_pinned_menu(chat_id: int) -> None:
    """
    Гарантирует наличие закреплённого сообщения-меню в групповом чате.
    • Если уже есть — обновляет разметку.
    • Иначе — распинивает всё, отправляет новое сообщение, пинает его и запоминает id.
    """
    bot = Bot.get_current()
    menu_id = group_menu_message_id.get(int(chat_id))
    markup = build_menu_markup()

    if menu_id:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=menu_id, reply_markup=markup)
            logger.debug("[guide] updated pinned menu %d in chat %d", menu_id, chat_id)
            return
        except Exception as e:
            logger.warning("[guide] failed to update menu %d in chat %d: %s", menu_id, chat_id, e)

    sent = await bot.send_message(
        chat_id,
        "📌 *Личный кабинет ведущего:*",
        parse_mode="Markdown",
        reply_markup=markup,
        disable_web_page_preview=True,
    )

    with contextlib.suppress(Exception):
        await bot.unpin_all_chat_messages(chat_id=chat_id)

    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
    except Exception as e:
        # не критично: сообщение отправлено, но не закрепилось — просто логируем
        logger.debug("[guide] pin_chat_message failed for chat_id=%s: %r", chat_id, e)

    group_menu_message_id[int(chat_id)] = int(sent.message_id)
    state.group_menu_message_id = group_menu_message_id  # синхронизируем обратно в state
    logger.info("[guide] pinned new menu %d in chat %d", sent.message_id, chat_id)


# ███ [4] Групповое меню для main.py
# --------------------------------------------------------------------
def group_keyboard() -> InlineKeyboardMarkup:
    return build_menu_markup()


# ███ [5] ОБРАБОТЧИКИ
# --------------------------------------------------------------------
@router.my_chat_member()
async def on_bot_join(evt: ChatMemberUpdated) -> None:
    """
    Когда бот добавлен в группу/назначен админом — создаём/обновляем закреплённое меню.
    """
    try:
        new_status = str(getattr(evt, "new_chat_member", None).status)  # type: ignore[union-attr]
    except Exception:
        new_status = ""
    if new_status in {"member", "administrator"}:
        logger.info("[guide] bot joined chat %d as %s", evt.chat.id, new_status)
        await ensure_pinned_menu(evt.chat.id)


@router.message(CommandStart())
async def on_group_start(message: Message) -> None:
    """
    Обработка /start в группе/супергруппе: гарантируем «закреп».
    """
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

# История изменений:
# • 2025-08-24 — v14.8: Pin/Unpin переведены на методы Bot (совместимость aiogram 3.x).
# • 2025-08-08 — v14.7: кастом-кнопки только в группах, без SkipHandler; остальное без изменений.
