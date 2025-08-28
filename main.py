"""main.py — точка входа MasterBot 15.22
────────────────────────────────────────────────────────────────────────────
• Пер-пользовательный мьютекс: события одного uid обрабатываются последовательно.
• «Слоты» сообщений в ЛС: status + dashboard (редактируем вместо размножения).
• Pre-vacuum один раз на апдейт; меню не удаляем.
• NormalizeButtons — нормализация текстов кнопок (пробелы/регистры/ё↔е/эмодзи),
  чтобы «📊 Отчёт по опросу» стабильно матчился и открывал дашборд.
• Anti-flicker: больше не удаляем входящее сообщение пользователя с кнопкой,
  и не редактируем слот/меню, если содержимое не меняется.
"""

from __future__ import annotations

# ── imports ─────────────────────────────────────────────────────────
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import time
import types
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message, TelegramObject, CallbackQuery, Update
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
from handlers.polls_lifecycle import _vacuum_old_messages  # фоновый пылесос ЛС

# side-routers
from handlers.confirmations import router as _r1  # noqa: F401
from handlers.stats          import router as _r2  # noqa: F401
from handlers.profile        import router as _r3  # noqa: F401
from handlers.my_games       import router as _r4  # noqa: F401
from handlers.bonuses        import router as _r5  # noqa: F401

logger = logging.getLogger(__name__)

# ── logging ─────────────────────────────────────────────────────────
LOG_DIR = Path(settings.LOG_DIR); LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "masterbot.log"

def _setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "DEBUG").upper()
    root_level = getattr(logging, level_name, logging.DEBUG)
    fmt = "%(asctime)s [%(levelname).1s] %(name)s:%(lineno)d — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: List[logging.Handler] = [
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=7, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=root_level, format=fmt, datefmt=datefmt, handlers=handlers)
    for pkg in ("core", "handlers", "services"):
        logging.getLogger(pkg).setLevel(logging.DEBUG if root_level == logging.DEBUG else logging.INFO)
    for noisy in ("aiosqlite", "googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)

_setup_logging()

# Контроль версий модулей
import handlers.my_games as _mg  # noqa: E402
import handlers.profile as _pf   # noqa: E402
logger.debug("✅ using my_games from %s", _mg.__file__)
logger.debug("✅ using profile  from %s", _pf.__file__)

# ── helpers / tracking ──────────────────────────────────────────────
_DASHBOARD_RX = re.compile(r"выберит[её]\s+игру.*детал", re.IGNORECASE | re.DOTALL)

def _is_volatile_text(text: Any) -> bool:
    t = (str(text or "")).lower()
    return ("игр пока нет" in t) or ("нет назначенных игр" in t)

def _is_dashboard_root(text: Any) -> bool:
    return bool(_DASHBOARD_RX.search(str(text or "")))

def _register_sent(chat_id: int, message_id: int, kind: str = "text") -> None:
    try:
        store: Dict[int, List[Tuple[int, float, str]]] = getattr(state, "sent_messages", {})  # type: ignore[assignment]
        if not isinstance(store, dict):
            store = {}; setattr(state, "sent_messages", store)
        bucket = store.setdefault(int(chat_id), [])
        bucket.append((int(message_id), float(time.monotonic()), str(kind)))
        if len(bucket) > 800:
            del bucket[: len(bucket) - 800]
    except Exception:
        pass

def _known_menu_ids(uid: int) -> set[int]:
    keep: set[int] = set()
    for attr in ("menu_message_id", "last_menu_message_id", "menu_messages"):
        val = getattr(state, attr, None)
        if isinstance(val, dict):
            mid = val.get(uid) or val.get(str(uid))
            if isinstance(mid, int):
                keep.add(mid)
            elif isinstance(mid, (list, tuple)):
                keep |= {int(x) for x in mid if isinstance(x, int)}
        elif isinstance(val, int):
            keep.add(val)
    # слоты не считаем «меню»
    for slot_name in ("status_message_id", "dashboard_message_id"):
        slot = getattr(state, slot_name, {})
        if isinstance(slot, dict):
            mid = slot.get(uid)
            if isinstance(mid, int):
                keep.discard(mid)
    return keep

def _protected_detail_ids(uid: int) -> set[int]:
    """
    Собирает ВСЕ message_id карточек деталей для данного uid, независимо от формата хранения:
    • новая схема ключа: (uid:int, deal_id:int) -> List[int]
    • легаси: detail_blocks[uid] или detail_blocks[str(uid)] -> List[int|Message]
    Эти id никогда не удаляем фоновым/предварительным пылесосом.
    """
    ids: set[int] = set()
    try:
        blocks = getattr(state, "detail_blocks", {}) or {}
        # новая схема ключей
        if isinstance(blocks, dict):
            for k, mids in list(blocks.items()):
                try:
                    if isinstance(k, tuple) and len(k) == 2 and int(k[0]) == int(uid):
                        for m in (mids or []):
                            try:
                                ids.add(int(getattr(m, "message_id", m)))
                            except Exception:
                                pass
                except Exception:
                    continue
            # легаси
            for lk in (uid, str(uid)):
                if lk in blocks:
                    for m in (blocks.get(lk) or []):
                        try:
                            ids.add(int(getattr(m, "message_id", m)))
                        except Exception:
                            pass
    except Exception:
        pass
    return ids

async def _vacuum_tracked_private(uid: int) -> None:
    """
    Чистит все отправленные ботом сообщения в личке, КРОМЕ:
    • известных message_id меню;
    • карточек деталей (detail-view) — защищены _protected_detail_ids(uid).
    """
    try:
        bot = Bot.get_current()
        store: Dict[int, List[Tuple[int, float, str]]] = getattr(state, "sent_messages", {})  # type: ignore[assignment]
        if not isinstance(store, dict):
            return
        items = list(store.get(int(uid), []) or [])
        keep_ids = _known_menu_ids(uid) | _protected_detail_ids(uid)
        new_items: List[Tuple[int, float, str]] = []
        for mid, ts, kind in items:
            if int(mid) in keep_ids:
                new_items.append((mid, ts, kind)); continue
            with contextlib.suppress(Exception):
                await bot.delete_message(int(uid), int(mid))
        store[int(uid)] = new_items
    except Exception as e:
        logger.debug("[vacuum-tracked-private] skip: %s", e)

async def _vacuum_detail_blocks(uid: int) -> None:
    """
    [LEGACY-совместимость]
    Раньше здесь удалялись сообщения деталей по ключам detail_blocks[uid].
    Теперь карточками деталей управляет handlers.poll_details, а пылесос «перед рендером»
    работает с точным списком keep (см. _protected_detail_ids). Здесь — no-op.
    """
    return

async def _vacuum_notify_feed(ttl_seconds: int = 60 * 30) -> None:
    """Удаляем уведомления в общих чатах по TTL.
    Теперь УДАЛЯЕМ и poll, и text (раньше poll оставляли)."""
    try:
        now_m = float(time.monotonic())
        bot = Bot.get_current()
        store: Dict[int, List[Tuple[int, float, str]]] = getattr(state, "sent_messages", {})  # type: ignore[assignment]
        if not isinstance(store, dict):
            return
        for chat_id, items in list(store.items()):
            if int(chat_id) > 0:
                continue  # только групповые/канальные (отриц. id)
            new_items: List[Tuple[int, float, str]] = []
            for mid, ts, kind in list(items):
                if (now_m - ts) < float(ttl_seconds):
                    new_items.append((mid, ts, kind)); continue
                with contextlib.suppress(Exception):
                    await bot.delete_message(int(chat_id), int(mid))
            store[int(chat_id)] = new_items
    except Exception as e:
        logger.debug("[vacuum-notify-feed] skip: %s", e)

# ── middleware: логгер ──────────────────────────────────────────────
class UpdateLogger(BaseMiddleware):
    async def __call__(self, handler: Any, event: TelegramObject, data: dict):
        t = type(event).__name__
        short = getattr(event, "text", "") or getattr(event, "data", "") or repr(event)
        short = (short[:60] + "…") if len(short) > 60 else short
        logger.info("[update] %-15s %s", t, short)
        return await handler(event, data)

# ── middleware: нормализатор кнопок ─────────────────────────────────
_SPACE_RE = re.compile(r"[\u00A0\u1680\u180E\u2000-\u200B\u202F\u205F\u3000]+")
MULTI_SPACE_RE = re.compile(r"\s+")
BTN_ALIASES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(?:📊\s*)?отч[её]т\s+по\s+опрос[ау]?\s*\.?\s*$", re.IGNORECASE), "📊 Отчёт по опросу"),
    (re.compile(r"^\s*(?:🎲\s*)?мои\s+игр[аы]\s*$", re.IGNORECASE), "🎲 Мои игры"),
    (re.compile(r"^\s*(?:📋\s*)?созд(?:ать|аём|аем)?\s+опрос\s*$", re.IGNORECASE), "📋 Создать опрос"),
]

def _normalize_text(s: str) -> str:
    s = _SPACE_RE.sub(" ", s)
    s = MULTI_SPACE_RE.sub(" ", s).strip()
    for rx, canon in BTN_ALIASES:
        if rx.match(s):
            return canon
    return s

# 👇 НОВОЕ: хелпер, чтобы различать человеческую подпись и машинный payload
_PAYLOADISH_RE = re.compile(r"[{}[\]=:|]|->|^poll:|^swap:|^act:|^cmd:|^detail:", re.IGNORECASE)
_ASCII_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:;=|,./{}\[\]\-+@#%]+$")

def _looks_like_payload(s: str) -> bool:
    # Имеет структурные символы / префиксы, либо токен без пробелов и кириллицы
    if _PAYLOADISH_RE.search(s):
        return True
    if _ASCII_TOKEN_RE.match(s):
        return True
    return False

class NormalizeButtons(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]):
        try:
            if isinstance(event, Message) and isinstance(event.text, str):
                # ТОЛЬКО текст пользователя — нормализуем агрессивно
                norm = _normalize_text(event.text)
                if norm != event.text:
                    logger.debug("[normalize] '%s' -> '%s'", event.text, norm)
                    event.text = norm  # type: ignore[attr-defined]

            elif isinstance(event, CallbackQuery) and isinstance(event.data, str):
                # ⚠️ ВНИМАНИЕ: не трогаем машинные payload'ы
                if _looks_like_payload(event.data):
                    # оставляем как есть — это данные для роутера/хэндлера
                    pass
                else:
                    norm = _normalize_text(event.data)
                    if norm != event.data:
                        logger.debug("[normalize] cq '%s' -> '%s'", event.data, norm)
                        event.data = norm  # type: ignore[attr-defined]

        except Exception as e:
            logger.debug("[normalize] skip: %s", e)
        return await handler(event, data)


# ── middleware: пер-пользовательский мьютекс ────────────────────────
_USER_LOCKS: Dict[int, asyncio.Lock] = {}
def _get_user_lock(uid: int) -> asyncio.Lock:
    lock = _USER_LOCKS.get(uid)
    if lock is None:
        lock = asyncio.Lock(); _USER_LOCKS[uid] = lock
    return lock

class PerUserMutex(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]) -> Any:
        uid: int | None = None
        if isinstance(event, Message) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, Update):
            if event.message and event.message.from_user:
                uid = event.message.from_user.id
            elif event.callback_query and event.callback_query.from_user:
                uid = event.callback_query.from_user.id
            elif event.poll_answer and event.poll_answer.user:
                uid = event.poll_answer.user.id
        if not isinstance(uid, int):
            return await handler(event, data)
        lock = _get_user_lock(uid)
        async with lock:
            return await handler(event, data)

# ── middleware: пылесос перед рендером (один раз на апдейт) ────────
class VacuumBeforeRender(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            target_uids: List[int] = []
            render_token = float(time.monotonic())

            def _mark(uid: int) -> None:
                tokens = getattr(state, "_render_token", {}) or {}
                tokens[uid] = render_token
                setattr(state, "_render_token", tokens)
                done = getattr(state, "_render_done", {}) or {}
                done.pop(uid, None)
                setattr(state, "_render_done", done)

            if isinstance(event, Message) and event.from_user:
                # ⚠️ Не удаляем входящее сообщение пользователя в ЛС — устраняет мигание.
                target_uids.append(event.from_user.id); _mark(event.from_user.id)
            elif isinstance(event, CallbackQuery) and event.from_user:
                target_uids.append(event.from_user.id); _mark(event.from_user.id)
            elif isinstance(event, Update):
                if event.message and event.message.from_user:
                    target_uids.append(event.message.from_user.id); _mark(event.message.from_user.id)
                if event.callback_query and event.callback_query.from_user:
                    target_uids.append(event.callback_query.from_user.id); _mark(event.callback_query.from_user.id)
                if event.poll_answer and event.poll_answer.user:
                    uid = event.poll_answer.user.id
                    target_uids.append(uid); _mark(uid)
                    lid = getattr(state, "current_poll_leader", None)
                    if isinstance(lid, int) and lid not in target_uids:
                        target_uids.append(lid); _mark(lid)

            for uid in {u for u in target_uids if isinstance(u, int)}:
                done = getattr(state, "_render_done", {})
                tokens = getattr(state, "_render_token", {})
                if isinstance(done, dict) and isinstance(tokens, dict):
                    if done.get(uid) == tokens.get(uid):
                        continue

                # ГЛАВНОЕ: защищаем detail-view от удаления
                keep_ids = list(_known_menu_ids(uid) | _protected_detail_ids(uid))
                try:
                    # новая сигнатура ядра: delete_previous_private_messages(bot, uid, keep=[...])
                    await delete_previous_private_messages(Bot.get_current(), uid, keep=keep_ids)  # type: ignore[arg-type]
                except TypeError:
                    # легаси-ядро без keep — минимальный режим
                    await delete_previous_private_messages(uid)

                # tracked-пылесос учитывает detail-ids сам
                await _vacuum_tracked_private(uid)

                done = getattr(state, "_render_done", {}) or {}
                done[uid] = tokens.get(uid)
                setattr(state, "_render_done", done)
        except Exception as e:
            logger.debug("[vacuum-mw] skip: %s", e)

        return await handler(event, data)

# ── startup ─────────────────────────────────────────────────────────
async def on_startup() -> None:
    state.config = (settings.model_dump() if hasattr(settings, "model_dump") else settings.dict())
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

# ── /start ──────────────────────────────────────────────────────────
def _ensure_menu_state() -> Dict[int, int]:
    cur = getattr(state, "menu_message_id", None)
    if isinstance(cur, dict):
        return cur
    setattr(state, "menu_message_id", {})
    return state.menu_message_id  # type: ignore[return-value]

def _ensure_menu_fp_state() -> Dict[int, str]:
    """Кэшим «отпечаток» последней раскладки меню на юзера, чтобы
    не дергать editMessageReplyMarkup без реального изменения клавиатуры."""
    cur = getattr(state, "_menu_kb_fp", None)
    if isinstance(cur, dict):
        return cur  # type: ignore[return-value]
    setattr(state, "_menu_kb_fp", {})
    return state._menu_kb_fp  # type: ignore[return-value]

def _kb_fingerprint(kb: Any) -> str:
    """Стабильный отпечаток клавиатуры для сравнения."""
    try:
        # aiogram v3 объекты — pydantic-модели
        if hasattr(kb, "model_dump_json"):
            return kb.model_dump_json(by_alias=True, exclude_none=True, sort_keys=True)  # type: ignore[no-any-return]
        if hasattr(kb, "model_dump"):
            import json as _json
            return _json.dumps(kb.model_dump(by_alias=True, exclude_none=True), sort_keys=True, ensure_ascii=False)
    except Exception:
        pass
    # как fallback — repr
    return repr(kb)

async def _send_main_menu(uid: int) -> None:
    kb = await get_main_menu(uid)
    bot = Bot.get_current()
    if not kb:
        msg = await bot.send_message(uid, "⛔ У вас пока нет доступа к функциям бота.")
        _register_sent(int(uid), int(msg.message_id), "text")
        return

    menu_map = _ensure_menu_state()
    fp_map = _ensure_menu_fp_state()
    new_fp = _kb_fingerprint(kb)
    mid = menu_map.get(uid)

    # Anti-flicker: если клавиатура идентичная — не дергаем editMessageReplyMarkup
    if isinstance(mid, int) and fp_map.get(uid) == new_fp:
        return

    if isinstance(mid, int):
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(uid, mid, reply_markup=kb)
            fp_map[uid] = new_fp
            return

    msg = await bot.send_message(uid, "Меню", reply_markup=kb)
    menu_map[uid] = int(msg.message_id)
    fp_map[uid] = new_fp
    _register_sent(int(uid), int(msg.message_id), "text")

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

# ── main ────────────────────────────────────────────────────────────
async def main() -> None:
    bot = Bot(token=settings.API_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
    Bot.get_current = classmethod(lambda cls: bot)  # type: ignore

    # one-time pre-vacuum helper
    async def _pre_vacuum_if_private(chat_id: int | str) -> None:
        try:
            cid = int(chat_id)
        except Exception:
            return
        if cid <= 0:
            return
        tokens = getattr(state, "_render_token", {})
        done = getattr(state, "_render_done", {})
        token = tokens.get(cid) if isinstance(tokens, dict) else None
        already = (isinstance(done, dict) and done.get(cid) == token)
        if already:
            return

        # Защищаем меню и detail-view
        keep_ids = list(_known_menu_ids(cid) | _protected_detail_ids(cid))
        try:
            await delete_previous_private_messages(Bot.get_current(), cid, keep=keep_ids)  # type: ignore[arg-type]
        except TypeError:
            await delete_previous_private_messages(cid)

        # detail-view не трогаем (_vacuum_detail_blocks — no-op)
        await _vacuum_tracked_private(cid)

        if isinstance(done, dict) and isinstance(tokens, dict):
            done[cid] = tokens.get(cid)
            setattr(state, "_render_done", done)

    # слоты + анти-фликер по тексту
    def _get_slot(name: str) -> Dict[int, int]:
        m = getattr(state, name, {})
        if not isinstance(m, dict):
            m = {}; setattr(state, name, m)
        return m  # type: ignore[return-value]

    def _get_slot_text(name: str) -> Dict[int, str]:
        t = getattr(state, "_slot_texts", {})
        if not isinstance(t, dict):
            t = {}
        if name not in t or not isinstance(t[name], dict):
            t[name] = {}
        setattr(state, "_slot_texts", t)
        return t[name]  # type: ignore[return-value]

    async def _send_or_edit_slot(slot_name: str, chat_id: int | str, text: str, **kwargs: Any) -> Any:
        """Редактируем слот (dashboard/status) без лишних правок текста."""
        cid = int(chat_id)
        slot = _get_slot(slot_name)
        last_text_map = _get_slot_text(slot_name)
        mid = slot.get(cid)
        last_text = last_text_map.get(cid)

        if isinstance(mid, int) and last_text == text:
            # текст не изменился — не трогаем текст; поправим только разметку (если нужно).
            try:
                return await bot.edit_message_reply_markup(
                    chat_id=cid,
                    message_id=mid,
                    reply_markup=kwargs.get("reply_markup"),
                )
            except Exception:
                pass  # если нечего редактировать — молча продолжаем

        if isinstance(mid, int):
            try:
                msg = await bot.edit_message_text(chat_id=cid, message_id=mid, text=text, **kwargs)
                last_text_map[cid] = text
                return msg
            except Exception:
                pass  # если не удалось — отправим новое

        msg = await bot._orig_send_message(chat_id, text, **kwargs)  # type: ignore[attr-defined]
        try:
            mid_new = int(getattr(msg, "message_id"))
            slot[cid] = mid_new
            last_text_map[cid] = text
            _register_sent(cid, mid_new, slot_name)
        except Exception:
            pass
        return msg

    # patch send_*
    bot._orig_send_message = bot.send_message  # type: ignore[attr-defined]
    bot._orig_send_poll    = bot.send_poll     # type: ignore[attr-defined]

    async def _patched_send_message(chat_id: int | str, *args: Any, **kwargs: Any):
        await _pre_vacuum_if_private(chat_id)
        text = kwargs.get("text") if "text" in kwargs else (args[0] if args else "")
        if int(chat_id) > 0:
            if _is_dashboard_root(text):
                return await _send_or_edit_slot("dashboard_message_id", chat_id, str(text), **kwargs)
            if _is_volatile_text(text):
                return await _send_or_edit_slot("status_message_id", chat_id, str(text), **kwargs)
        msg = await bot._orig_send_message(chat_id, *args, **kwargs)  # type: ignore[attr-defined]
        try:
            _register_sent(int(chat_id), int(msg.message_id), "text")
        except Exception:
            pass
        return msg

    async def _patched_send_poll(chat_id: int | str, *args: Any, **kwargs: Any):
        await _pre_vacuum_if_private(chat_id)
        msg = await bot._orig_send_poll(chat_id, *args, **kwargs)  # type: ignore[attr-defined]
        try:
            _register_sent(int(chat_id), int(msg.message_id), "poll")
        except Exception:
            pass
        return msg

    bot.send_message = _patched_send_message  # type: ignore[assignment]
    bot.send_poll    = _patched_send_poll     # type: ignore[assignment]

    # глобальный перехватчик (answer()/reply())
    _orig_call = bot.__call__
    async def _patched_call(self: Bot, method: Any):
        try:
            method_name = getattr(method, "__class__", type(method)).__name__
            chat_id = getattr(method, "chat_id", None)
            text = getattr(method, "text", None)
            if method_name in {"SendMessage", "SendPoll"} and chat_id is not None:
                await _pre_vacuum_if_private(chat_id)
                if method_name == "SendMessage" and int(chat_id) > 0:
                    if _is_dashboard_root(text):
                        kw = {k: getattr(method, k) for k in ("parse_mode", "reply_markup") if hasattr(method, k)}
                        return await _send_or_edit_slot("dashboard_message_id", chat_id, str(text), **kw)
                    if _is_volatile_text(text):
                        kw = {k: getattr(method, k) for k in ("parse_mode", "reply_markup") if hasattr(method, k)}
                        return await _send_or_edit_slot("status_message_id", chat_id, str(text), **kw)
            result = await _orig_call(method)
            if method_name in {"SendMessage", "SendPoll"} and chat_id is not None:
                mid = getattr(result, "message_id", None)
                if mid is not None:
                    _register_sent(int(chat_id), int(mid), "poll" if method_name == "SendPoll" else "text")
            return result
        except Exception:
            return await _orig_call(method)
    bot.__call__ = types.MethodType(_patched_call, bot)

    # dispatcher
    dp = Dispatcher()
    dp.startup.register(on_startup)

    # порядок важен: мьютекс → нормализатор → пылесос → логгер
    dp.update.middleware(PerUserMutex())
    dp.update.middleware(NormalizeButtons())
    dp.update.middleware(VacuumBeforeRender())
    dp.update.middleware(UpdateLogger())

    dp.message.register(group_start, CommandStart(), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    dp.message.register(private_start, CommandStart(), F.chat.type == ChatType.PRIVATE)
    dp.message.register(legacy_profile_start, CommandStart())

    setup_handlers(dp)
    dp.include_router(guide_router)
    logger.info("[setup] routers registered: %d", len(getattr(dp, "sub_routers", [])))

    # scheduler
    scheduler = AsyncIOScheduler(timezone=timezone("Europe/Moscow"))
    scheduler.add_job(_vacuum_old_messages, "interval", minutes=15)   # ЛС (ядро)
    scheduler.add_job(_vacuum_notify_feed, "interval", minutes=10)    # общие чаты по TTL
    scheduler.start()

    logger.info("🤖 Bot starting, version=%s", settings.VERSION)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        scheduler.shutdown()
        logger.info("Bot stopped")

# ── self-test ───────────────────────────────────────────────────────
async def _test() -> None:
    from core.db import get_user_info as _orig_get_user_info
    async def _fake(_uid: int) -> dict:
        return {"role": settings.ACCESS["poll"][0]}
    import core.db as _db
    _db.get_user_info = _fake  # type: ignore
    kb = await get_main_menu(1)
    assert kb and len(kb.keyboard) > 0
    _db.get_user_info = _orig_get_user_info  # type: ignore
    print("main.py smoke-test OK")

# ── entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
    except Exception:
        logger.exception("Fatal error")