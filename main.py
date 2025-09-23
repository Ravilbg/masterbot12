"""main.py — точка входа MasterBot 15.23
────────────────────────────────────────────────────────────────────────────
• Пер-пользовательский мьютекс: события одного uid обрабатываются последовательно.
• «Слоты» сообщений в ЛС: status + dashboard (редактируем вместо размножения).
• Pre-vacuum один раз на апдейт; меню не удаляем.
• NormalizeButtons — нормализация текстов кнопок (пробелы/регистры/ё↔е/эмодзи),
  чтобы «📊 Отчёт по опросу» стабильно матчился и открывал дашборд.
• Anti-flicker: не редактируем слот/меню, если содержимое не меняется.
• NEW: Чистый экран — входящие сообщения в ЛС удаляются; на произвольный текст
  показываем подсказку и тут же стираем её.
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message, TelegramObject, CallbackQuery, Update
from aiogram.exceptions import TelegramUnauthorizedError  # ← логирование Unauthorized
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

from core.config import settings
from core.db import init_db
from core.menu import get_main_menu, send_root_menu_singleton
from core.state import state
from core.utils import dm_singleton_send
from core.utils import vacuum_private as ssot_vacuum_private, keep_for_vacuum as ssot_keep_for_vacuum
from handlers import setup as setup_handlers
from handlers.guide import group_keyboard, router as guide_router
from handlers.profile import profile_handler
from handlers.polls_lifecycle import _vacuum_old_messages
from handlers import guide  # noqa

# Rating system imports (добавлены для устранения ошибок)
try:
    from services.ratings import (
        get_all_leader_uids, has_flag, get_flag, set_flag, record_event,
        get_cant_work_weeks, recompute_baseline_for_all
    )
except ImportError:
    # Fallback functions if ratings module is not available
    async def get_all_leader_uids(): return []
    async def has_flag(*args): return False
    async def get_flag(*args): return None
    async def set_flag(*args): pass
    async def record_event(*args): pass
    async def get_cant_work_weeks(): return {}
    async def recompute_baseline_for_all(): pass

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
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    root_level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s [%(levelname).1s] %(name)s:%(lineno)d — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: List[logging.Handler] = [
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=7, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=root_level, format=fmt, datefmt=datefmt, handlers=handlers)
    for pkg in ("core", "handlers", "services"):
        logging.getLogger(pkg).setLevel(logging.INFO)
    for noisy in ("aiosqlite", "googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)

_setup_logging()

# Контроль версий модулей (убираем лишние логи)
import handlers.my_games as _mg  # noqa: E402
import handlers.profile as _pf   # noqa: E402

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

def _protected_detail_ids(uid: int) -> set[int]:
    ids: set[int] = set()
    try:
        blocks = getattr(state, "detail_blocks", {}) or {}
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
    try:
        bot = Bot.get_current()
        store: Dict[int, List[Tuple[int, float, str]]] = getattr(state, "sent_messages", {})  # type: ignore[assignment]
        if not isinstance(store, dict):
            return
        items = list(store.get(int(uid), []) or [])
        keep_ids = _protected_detail_ids(uid)
        # дополнительно бережём главное меню и sticky «Мои игры»
        try:
            from core.menu import get_menu_message_id as _menu_id  # lazy
            mid = _menu_id(int(uid))
            if isinstance(mid, int) and mid > 0:
                keep_ids.add(int(mid))
        except Exception:
            pass
        try:
            from core.utils import keep_for_vacuum as _keep_for_vacuum  # lazy
            for x in _keep_for_vacuum(int(uid)):
                try:
                    keep_ids.add(int(x))
                except Exception:
                    pass
        except Exception:
            pass
        new_items: List[Tuple[int, float, str]] = []
        for mid, ts, kind in items:
            if int(mid) in keep_ids:
                new_items.append((mid, ts, kind)); continue
            with contextlib.suppress(Exception):
                await bot.delete_message(int(uid), int(mid))
        store[int(uid)] = new_items
    except Exception:
        pass

async def _vacuum_detail_blocks(uid: int) -> None:
    return



async def _apply_no_reply_penalties() -> None:
    try:
        staff = set(int(uid) for uid in await get_all_leader_uids())
    except Exception:
        pass
        return
    responses = getattr(state, "responses", {}) or {}
    if not staff or not responses:
        return
    now_ts = int(time.time())
    for poll_id, payload in responses.items():
        try:
            opened_at = int(payload.get("opened_at") or 0)
        except Exception:
            opened_at = 0
        if opened_at <= 0 or now_ts < opened_at + 48 * 3600:
            continue
        responded: set[int] = set()
        try:
            deal_map = payload.get("deals") or {}
            for entries in list(deal_map.values()):
                for item in entries or []:
                    responded.add(int(item.get("user_id", 0)))
            for bucket in (payload.get("not_available"), payload.get("admin_available")):
                for item in bucket or []:
                    responded.add(int(item.get("user_id", 0)))
        except Exception:
            pass
        missing = {uid for uid in staff if uid not in responded}
        if not missing:
            continue
        poll_id_str = str(poll_id)
        for uid in sorted(missing):
            try:
                if await has_flag(uid, poll_id_str, "no_reply_w1"):
                    continue
                prev_poll = await get_flag(uid, "no_reply", "last_poll")
                consecutive = False
                if prev_poll and prev_poll != poll_id_str:
                    if await has_flag(uid, prev_poll, "no_reply_w1"):
                        consecutive = True
                await set_flag(uid, poll_id_str, "no_reply_w1")
                await set_flag(uid, "no_reply", "last_poll", poll_id_str)
                if consecutive:
                    if await has_flag(uid, poll_id_str, "no_reply_w2"):
                        continue
                    await record_event(
                        uid,
                        "no_reply_penalty_w2",
                        {"poll_id": poll_id_str},
                        poll_id=poll_id_str,
                    )
                    await set_flag(uid, poll_id_str, "no_reply_w2")
                    amount = 30
                else:
                    await record_event(
                        uid,
                        "no_reply_penalty_w1",
                        {"poll_id": poll_id_str},
                        poll_id=poll_id_str,
                    )
                    amount = 20
                text = (
                    f"⚠️ Рейтинг снижен на {amount}: вы не откликались в течение 48 часов после старта опроса.\n"
                    "Совет: отмечайте «могу/не могу» в первые 12–24 часа (+2 к рейтингу)."
                )
                await dm_singleton_send(uid, text, context="rating_penalty")
            except Exception:
                pass


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def _apply_cant_work_penalties() -> None:
    try:
        weeks_map = await get_cant_work_weeks()
    except Exception:
        pass
        weeks_map = {}
    if not weeks_map:
        await recompute_baseline_for_all()
        return
    now = datetime.now(tz=timezone("Europe/Moscow"))
    week0 = _week_key(now)
    week1 = _week_key(now - timedelta(weeks=1))
    week2 = _week_key(now - timedelta(weeks=2))
    for uid, weeks in weeks_map.items():
        if not isinstance(weeks, list):
            continue
        weeks_set = {str(w) for w in weeks}
        if week0 not in weeks_set:
            continue
        flag_poll = f"cantwork_{week0}"
        try:
            if week0 in weeks_set and week1 in weeks_set and week2 in weeks_set:
                if not await has_flag(uid, flag_poll, "cant_work_3w_row"):
                    await record_event(
                        uid,
                        "cant_work_3w_row",
                        {"weeks": [week2, week1, week0]},
                        poll_id=flag_poll,
                    )
                    await set_flag(uid, flag_poll, "cant_work_3w_row")
                    continue
            if week0 in weeks_set and week1 in weeks_set:
                if await has_flag(uid, flag_poll, "cant_work_3w_row"):
                    continue
                if not await has_flag(uid, flag_poll, "cant_work_2w_row"):
                    await record_event(
                        uid,
                        "cant_work_2w_row",
                        {"weeks": [week1, week0]},
                        poll_id=flag_poll,
                    )
                    await set_flag(uid, flag_poll, "cant_work_2w_row")
        except Exception:
            pass
    try:
        await recompute_baseline_for_all()
    except Exception:
        pass
async def _vacuum_notify_feed(ttl_seconds: int = 60 * 30) -> None:
    try:
        now_m = float(time.monotonic())
        bot = Bot.get_current()
        store: Dict[int, List[Tuple[int, float, str]]] = getattr(state, "sent_messages", {})  # type: ignore[assignment]
        if not isinstance(store, dict):
            return
        for chat_id, items in list(store.items()):
            if int(chat_id) > 0:
                continue
            new_items: List[Tuple[int, float, str]] = []
            for mid, ts, kind in list(items):
                if (now_m - ts) < float(ttl_seconds):
                    new_items.append((mid, ts, kind)); continue
                with contextlib.suppress(Exception):
                    await bot.delete_message(int(chat_id), int(mid))
            store[int(chat_id)] = new_items
    except Exception:
        pass

# ── middleware: логгер ──────────────────────────────────────────────
class UpdateLogger(BaseMiddleware):
    async def __call__(self, handler: Any, event: TelegramObject, data: dict):
        # Убираем логирование каждого обновления
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

_PAYLOADISH_RE = re.compile(r"[{}[\]=:|]|->|^poll:|^swap:|^act:|^cmd:|^detail:", re.IGNORECASE)
_ASCII_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:;=|,./{}\[\]\-+@#%]+$")

def _looks_like_payload(s: str) -> bool:
    if _PAYLOADISH_RE.search(s):
        return True
    if _ASCII_TOKEN_RE.match(s):
        return True
    return False

class NormalizeButtons(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]):
        try:
            if isinstance(event, Message) and isinstance(event.text, str):
                norm = _normalize_text(event.text)
                if norm != event.text:
                    event.text = norm  # type: ignore[attr-defined]
            elif isinstance(event, CallbackQuery) and isinstance(event.data, str):
                if not _looks_like_payload(event.data):
                    norm = _normalize_text(event.data)
                    if norm != event.data:
                        event.data = norm  # type: ignore[attr-defined]
        except Exception:
            pass
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

                keep_ids = list(set(ssot_keep_for_vacuum(uid)) | _protected_detail_ids(uid))
                try:
                    from core.menu import get_menu_message_id as _menu_id  # lazy
                    _mid = _menu_id(int(uid))
                except Exception:
                    _mid = None
                # критично: явно добавим menu_mid в keep_ids глобального пылесоса
                if isinstance(_mid, int) and _mid > 0 and _mid not in keep_ids:
                    keep_ids.append(int(_mid))
                try:
                    import logging as _l
                    _l.getLogger(__name__).info(
                        "[VacuumBeforeRender] uid=%s keep_ids=%s menu_mid=%r", int(uid), sorted(keep_ids), _mid
                    )
                except Exception:
                    pass
                try:
                    try:
                        await ssot_vacuum_private(Bot.get_current(), uid, keep=keep_ids)  # type: ignore[arg-type]
                    except TypeError:
                        try:
                            await ssot_vacuum_private(uid, keep=keep_ids)  # type: ignore[arg-type]
                        except TypeError:
                            await ssot_vacuum_private(uid)  # type: ignore[arg-type]
                except Exception:
                    await _vacuum_tracked_private(uid)

                await _vacuum_tracked_private(uid)

                done = getattr(state, "_render_done", {}) or {}
                done[uid] = tokens.get(uid)
                setattr(state, "_render_done", done)
        except Exception:
            pass

        return await handler(event, data)

# ███ [1.7] CLEAN DM MIDDLEWARE — авто-удаление входящих сообщений в ЛС
# ────────────────────────────────────────────────────────────────────
# Версия 1.0 · 2025-08-28
class _CleanPrivateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]):
        result = await handler(event, data)
        try:
            if isinstance(event, Message) and event.chat and event.chat.type == "private":
                bot: Bot | None = data.get("bot")
                if bot:
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id=event.chat.id, message_id=event.message_id)
        except Exception:
            pass
        return result

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

    await init_db()
    
    # Инициализация оптимизаций производительности
    try:
        from init_performance import init_performance_optimizations
        init_performance_optimizations()
    except Exception as e:
        logger.warning("Performance optimizations not loaded: %s", e)

# ── /start ──────────────────────────────────────────────────────────


async def _send_main_menu(uid: int) -> None:
    kb = await get_main_menu(uid)
    if kb:
        await send_root_menu_singleton(uid, kb)
    else:
        await dm_singleton_send(uid, "⛔ У вас пока нет доступа к функциям бота.")

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
        await profile_handler(message)
        await _send_main_menu(message.from_user.id)

# ███ [13.99] FALLBACK: «Воспользуйтесь командами главного меню» + авто-стирка
fallback_router = Router(name="fallback-clean")

@fallback_router.message(F.chat.type == "private")
async def _fallback_clean(message: Message, bot: Bot) -> None:
    tip = await message.answer("Воспользуйтесь командами главного меню")
    await asyncio.sleep(1.2)
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=message.chat.id, message_id=tip.message_id)

# ── helpers: token resolve & logging ────────────────────────────────
def _token_fingerprint(tok: str) -> str:
    tok = tok or ""
    if not tok:
        return "∅"
    head = tok[:8]
    tail = tok[-6:] if len(tok) > 6 else tok
    return f"{head}…{tail}"

def _read_token_from_config_json() -> str:
    """Фолбэк: ищем токен в config.json рядом с main.py/проектом. Читаем с utf-8-sig (BOM-safe)."""
    candidates = [
        Path(__file__).with_name("config.json"),
        Path.cwd() / "config.json",
        Path(__file__).parent.parent / "config.json",
    ]
    for p in candidates:
        try:
            if p.exists():
                data = json.loads(p.read_text("utf-8-sig"))
                # Приоритет: DEV_API_TOKEN для разработки, затем обычный токен
                v = (data.get("DEV_API_TOKEN") or data.get("API_TOKEN") or data.get("TELEGRAM_BOT_TOKEN") or "").strip()
                if isinstance(v, str) and v:
                    token_type = "DEV_API_TOKEN" if data.get("DEV_API_TOKEN") else "API_TOKEN"
                    logger.debug("[startup] token loaded from %s (%s)", p, token_type)
                    return v
        except Exception as e:
            logger.debug("[startup] skip reading %s: %s", p, e)
    return ""

def _resolve_api_token() -> tuple[str, str, Dict[str, str]]:
    """Возвращает (token, source, debug_meta). Источники: settings → env → config.json"""
    settings_tok = (settings.API_TOKEN or "").strip()
    env_tok = (os.getenv("DEV_API_TOKEN") or os.getenv("API_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    cfg_tok = _read_token_from_config_json()
    if settings_tok:
        src = "settings"
        tok = settings_tok
    elif env_tok:
        src = "env"
        tok = env_tok
    else:
        src = "config.json"
        tok = cfg_tok
    meta = {
        "settings_len": str(len(settings_tok)),
        "env_len": str(len(env_tok)),
        "config_len": str(len(cfg_tok)),
        "final_len": str(len(tok)),
        "settings_fp": _token_fingerprint(settings_tok),
        "env_fp": _token_fingerprint(env_tok),
        "config_fp": _token_fingerprint(cfg_tok),
        "final_fp": _token_fingerprint(tok),
    }
    return tok, src, meta

# ── main ────────────────────────────────────────────────────────────
async def main() -> None:
    # Безопасная резолва и валидация токена:
    token, source, meta = _resolve_api_token()

    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_\-]{20,}", token):
        logger.error("[startup] API_TOKEN missing/malformed (len=%s, fp=%s, source=%s)", meta["final_len"], meta["final_fp"], source)
        raise RuntimeError("API_TOKEN missing/malformed")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="Markdown"))
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
        token_ = tokens.get(cid) if isinstance(tokens, dict) else None
        already = (isinstance(done, dict) and done.get(cid) == token_)
        if already:
            return
        keep_ids = list(set(ssot_keep_for_vacuum(cid)) | _protected_detail_ids(cid))
        try:
            try:
                await ssot_vacuum_private(Bot.get_current(), cid, keep=keep_ids)  # type: ignore[arg-type]
            except TypeError:
                try:
                    await ssot_vacuum_private(cid, keep=keep_ids)  # type: ignore[arg-type]
                except TypeError:
                    await ssot_vacuum_private(cid)  # type: ignore[arg-type]
        except Exception:
            await _vacuum_tracked_private(cid)
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
        cid = int(chat_id)
        slot = _get_slot(slot_name)
        last_text_map = _get_slot_text(slot_name)
        mid = slot.get(cid)
        last_text = last_text_map.get(cid)

        if isinstance(mid, int) and last_text == text:
            try:
                return await bot.edit_message_reply_markup(
                    chat_id=cid,
                    message_id=mid,
                    reply_markup=kwargs.get("reply_markup"),
                )
            except Exception:
                pass

        if isinstance(mid, int):
            try:
                msg = await bot.edit_message_text(chat_id=cid, message_id=mid, text=text, **kwargs)
                last_text_map[cid] = text
                return msg
            except Exception:
                pass

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
    dp.message.middleware(_CleanPrivateMiddleware())

    dp.message.register(group_start, CommandStart(), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    dp.message.register(private_start, CommandStart(), F.chat.type == ChatType.PRIVATE)
    dp.message.register(legacy_profile_start, CommandStart())

    setup_handlers(dp)
    dp.include_router(guide_router)
    dp.include_router(fallback_router)

    # scheduler (тихий режим)
    import logging
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    
    scheduler = AsyncIOScheduler(timezone=timezone("Europe/Moscow"))
    scheduler.add_job(_vacuum_old_messages, "interval", minutes=15)
    scheduler.add_job(_vacuum_notify_feed, "interval", minutes=10)
    scheduler.add_job(_apply_no_reply_penalties, "interval", hours=3)
    weekly_cron = str((settings.RATING or {}).get("WEEKLY_CHECK_CRON", "0 6 * * MON"))
    cron_parts = weekly_cron.split()
    if len(cron_parts) == 5:
        cron_minute, cron_hour, cron_day, cron_month, cron_dow = cron_parts
    else:
        cron_minute, cron_hour, cron_day, cron_month, cron_dow = "0", "6", "*", "*", "mon"
    scheduler.add_job(
        _apply_cant_work_penalties,
        "cron",
        minute=cron_minute,
        hour=cron_hour,
        day=cron_day,
        month=cron_month,
        day_of_week=str(cron_dow).lower(),
    )
    scheduler.start()

    # Очистка очереди обновлений для тихого запуска
    try:
        await bot.get_updates(offset=-1, limit=1)
    except Exception:
        pass

    logger.info("🤖 Bot starting, version=%s", settings.VERSION)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError as e:
        # Доп. диагностика в логи — источник токена, отпечаток и подсказка
        logger.error(
            "[auth] Telegram Unauthorized on getMe: %s | token_fp=%s | source=%s | len=%s",
            getattr(e, "message", repr(e)),
            meta["final_fp"],  # безопасный отпечаток
            source,
            meta["final_len"],
        )
        logger.error(
            "[auth] HINT: проверьте, что переменные окружения не перекрывают config.json; "
            "сравните fingerprints (settings|env|config|final): %s | %s | %s | %s",
            meta["settings_fp"], meta["env_fp"], meta["config_fp"], meta["final_fp"],
        )
        raise
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
    assert kb and len(kb.keyboard) > 0  # type: ignore[attr-defined]
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


# 2025-09-17 · модуль рейтинга: выровнено под SSOT.

