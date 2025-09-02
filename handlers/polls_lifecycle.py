# handlers/polls_lifecycle.py — цикл опроса/распределения
# ─────────────────────────────────────────────────────────────────────
"""
Создание опросов, приём ответов, отчёт лидеру и служебные утилиты.

Версия 6.5 · 2025-08-14
──────────────────────────────────────────────────────────────────────
• FIX: добавлен _sync_leader_report + sync_report (обратная совместимость).
• FIX: «пылесос» ЛС удаляет всё, кроме текущей группы сообщений (keep).
• FIX: устойчивые импорты, заглушки, отсутствие циклических зависимостей.
• FIX: ready-счёт с защитой от дублей uid (1 пользователь = 1 роль).
• NEW: автораспределение из ответов опроса → ранний апдейт distribution_cache.
• NEW: кнопки «👍 Утвердить», «✅ Утверждено», «Утвердить все» в дашборде.
• PERF: параллельная перерисовка detail-вью; экономные проходы по state.
• Совместимость c handlers.poll_details (распределение/инварианты) и
  handlers.polls_distribution (утверждение, действия в отчёте).
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import contextlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Set, Tuple, Optional, Iterable, TYPE_CHECKING

from aiogram import Bot, Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder  # резерв под дальнейшее
from pytz import timezone
from difflib import SequenceMatcher

# ── core.* (с фолбэками на ранние сборки) ───────────────────────────
try:
    from core.config import settings  # type: ignore
except Exception:
    from types import SimpleNamespace
    settings = SimpleNamespace(  # type: ignore
        POLLS_CHAT_ID=None,
        LEADERS_CHAT_ID=None,
        ADMIN_CHAT_ID=None,
        DATE_FILTER_DAYS=30,
        POLL_DURATION_HOURS=24,
        GAME_ROLE_MAPPING={},
        NEW_GAMES_STATUS_IDS=[],
        BRON_STATUS_ID=None,
        PRE_APPLICATION_STATUS_ID=None,
        SUCCESSFUL_STATUS_ID=None,
        POLL_WINDOW_DAYS=10,
        ACCESS={"poll": ["admin", "leader"]},
    )

try:
    from core.db import get_user_info, get_all_leader_uids  # type: ignore
except Exception:
    async def get_user_info(*_: Any, **__: Any) -> Dict[str, Any]:
        return {}
    async def get_all_leader_uids(*_: Any, **__: Any) -> List[int]:
        return []

try:
    from core.state import state  # type: ignore
except Exception:
    # Минимальная заглушка состояния (для ранних сборок/тестов).
    class _StateStub:
        def __init__(self) -> None:
            self.coordination_cycle_active: bool = False
            self.force_closed: bool = False
            self.deal_force_closed: Set[int] = set()
            self.current_poll_deals: List[dict] = []
            self.responses: Dict[str, dict] = {}
            self.distribution_cache: Dict[str, Dict[str, Any]] = {}
            self.poll_distribution: Dict[int, Dict[str, Any]] = {}
            self.current_deal_ready: Dict[int, bool] = {}
            self.all_ready_notified: bool = False
            self.last_user_messages: Dict[int, List[Any]] = {}
            self.messages_to_delete: Dict[int, List[int]] = {}
            self.detail_blocks: Dict[Tuple[int, int], List[Any]] = {}
            self.current_poll_leader: Optional[int] = None
            self.personal_report_message_id: Optional[int] = None
            self.reminder_tasks: List[object] = []
            self.admin_chat_id: Optional[int] = None
            self.assigned_index: Dict[int, Set[int]] = {}
            self.locked_distribution: Dict[int, Any] = {}
            # 👇 добавлено — текущий UI-контекст на пользователя
            self.ui_context: Dict[int, str] = {}
            # 👇 буфер «новые игры» для кнопки «Новая игра» в активном цикле
            self.pending_new_deals: List[Dict[str, Any]] = []
    state = _StateStub()  # type: ignore

# История изменений: 
# 2025-08-20 — блок выровнен; добавлен ui_context в заглушку state.
# 2025-08-29 — расширены заглушки settings (BRON_STATUS_ID, PRE_APPLICATION_STATUS_ID, SUCCESSFUL_STATUS_ID, POLL_WINDOW_DAYS);
#              добавлен pending_new_deals в заглушку state.

try:
    from core.utils import delete_previous_private_messages, truncate  # type: ignore
except Exception:
    async def delete_previous_private_messages(*_: Any, **__: Any) -> None:
        return None
    def truncate(text: str, max_len: int = 4096) -> str:
        return text if len(text) <= max_len else text[: max_len - 1] + "…"

# ── handlers.* (guard от циклических импортов и отсутствия модулей) ─
try:
    from handlers.games import _delete_trigger, _refresh_menu  # type: ignore
except Exception:
    async def _delete_trigger(message: types.Message) -> None:
        with contextlib.suppress(Exception):
            await message.delete()
    async def _refresh_menu(*_: Any, **__: Any) -> None:
        return None

try:
    from handlers.guide import PROFILE_BUTTON_TEXT  # type: ignore
except Exception:
    PROFILE_BUTTON_TEXT = "🎲 Личный кабинет"

# detail-view может отсутствовать в ранних сборках
try:
    from handlers.poll_details import refresh_deal_details  # type: ignore
except Exception:
    async def refresh_deal_details(*_: Any, **__: Any) -> Optional[Dict[str, Any]]:
        return None

# ── services.* ───────────────────────────────────────────────────────
try:
    from services.amocrm import get_amocrm_deals  # type: ignore
except Exception:
    async def get_amocrm_deals(*_: Any, **__: Any) -> List[Dict[str, Any]]:
        return []

try:
    from services.gsheets import get_user_status_from_svetofor  # type: ignore
except Exception:
    async def get_user_status_from_svetofor(*_: Any, **__: Any) -> Dict[str, str]:
        return {}

# ── настройка модуля ────────────────────────────────────────────────
logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")

# История изменений:
#   • 2025-08-20 — v6.5-fix: удалён мусор в середине блока, выровнены импорты под SSOT, Pylance-типы сохранены.
#   • 2025-08-29 — v6.5-fix2: добавлены недостающие поля настроек и состояния под «Новая игра» и окно POLL_WINDOW_DAYS.



# ════════════════════════════════════════════════════════════════════
# [0.90] TYPE HINTS SHIMS (только для анализатора)
# ════════════════════════════════════════════════════════════════════
if TYPE_CHECKING:
    async def clear_poll_data(uid: int) -> None: ...
    async def _sync_leader_report(leader_id: Optional[int] = None) -> None: ...
    async def _refresh_detail_views(impacted: Set[int], refresh_all: bool = False) -> None: ...
    async def generate_poll_report() -> str: ...
    def _build_report_keyboard() -> Any: ...

# ════════════════════════════════════════════════════════════════════
# [0.96] REPORT + VACUUM (edit-in-place, hard pre-vacuum, SSOT)
# ════════════════════════════════════════════════════════════════════

import contextlib
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import vacuum_private, delete_previous_private_messages  # SSOT

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# helpers: жёсткая очистка отчёта и detail-реестра (НЕ трогаем «Мои игры»)
# ────────────────────────────────────────────────────────────────────

def _collect_user_detail_entries(uid: int) -> Tuple[List[int], List[Tuple[int, int]]]:
    """
    Возвращает (message_ids для удаления, список tuple-ключей (uid, deal) для pop()).
    Поддерживает оба формата хранения:
      • старый: state.detail_blocks[uid] = {deal_id: [mids]}
      • SSOT : state.detail_blocks[(uid, deal_id)] = [mids]
    """
    mids: List[int] = []
    tuple_keys_to_pop: List[Tuple[int, int]] = []

    raw = getattr(state, "detail_blocks", {}) or {}
    if not isinstance(raw, dict):
        return mids, tuple_keys_to_pop

    # 1) SSOT-ключи (uid, deal_id)
    for k, v in list(raw.items()):
        if isinstance(k, tuple) and len(k) == 2:
            try:
                k_uid, k_deal = int(k[0]), int(k[1])
            except Exception:
                continue
            if k_uid != int(uid):
                continue
            if isinstance(v, list):
                for m in v:
                    with contextlib.suppress(Exception):
                        mids.append(int(m))
            elif isinstance(v, int):
                mids.append(int(v))
            tuple_keys_to_pop.append((k_uid, k_deal))

    # 2) Старый формат detail_blocks[uid] = {deal_id: [mids]}
    old_bucket = raw.get(uid) if isinstance(raw.get(uid), dict) else None  # type: ignore[index]
    if isinstance(old_bucket, dict):
        for vv in old_bucket.values():
            if isinstance(vv, list):
                for m in vv:
                    with contextlib.suppress(Exception):
                        mids.append(int(m))
            elif isinstance(vv, int):
                mids.append(int(vv))

    return mids, tuple_keys_to_pop


async def _pre_render_vacuum(uid: int) -> None:
    """
    Перед показом отчёта очищаем ТОЛЬКО:
      • все detail-сообщения пользователя (и их реестр/индексы),
      • предыдущий инстанс отчёта (если есть).
    Главное меню и дашборд «Мои игры» сохраняем.
    """
    bot = Bot.get_current()

    # 0) Удаляем прошлый отчёт (если есть локальный id)
    prev_report_id = getattr(state, "personal_report_message_id", None)
    if isinstance(prev_report_id, int) and prev_report_id > 0:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=uid, message_id=prev_report_id)
        setattr(state, "personal_report_message_id", None)
        logger.debug("[report:vacuum] previous report removed uid=%s mid=%s", uid, prev_report_id)

    # 1) Удаляем все зарегистрированные detail-сообщения пользователя (оба формата ключей)
    try:
        mids_to_delete, tuple_keys = _collect_user_detail_entries(uid)

        # удалить сами сообщения
        if mids_to_delete:
            logger.debug("[report:vacuum] delete details uid=%s mids=%s", uid, mids_to_delete)
            for mid in mids_to_delete:
                with contextlib.suppress(TelegramBadRequest, Exception):
                    await bot.delete_message(chat_id=uid, message_id=mid)

        # очистить старый формат detail_blocks[uid]
        db = getattr(state, "detail_blocks", None)
        if isinstance(db, dict):
            if uid in db and isinstance(db.get(uid), dict):  # type: ignore[index]
                db.pop(uid, None)  # старый формат
            # и SSOT-ключи (uid, deal_id)
            for k in tuple_keys:
                db.pop(k, None)  # type: ignore[index]
            logger.debug("[report:vacuum] registry cleared for uid=%s (formats: old+tuple)", uid)

        # подчистить detail_index — тоже поддерживаем оба формата
        idx = getattr(state, "detail_index", None)
        if isinstance(idx, dict):
            # SSOT
            for k in list(idx.keys()):
                if isinstance(k, tuple) and len(k) == 2:
                    with contextlib.suppress(Exception):
                        if int(k[0]) == int(uid):
                            idx.pop(k, None)
            # на случай старого формата detail_index[uid] = {...}
            if uid in idx:
                idx.pop(uid, None)  # type: ignore[index]
    except Exception as e:
        logger.debug("[report:vacuum] detail removal skipped uid=%s: %s", uid, e)

    # 2) Бережная чистка «хвостов»: сохраняем главное меню и активный дашборд «Мои игры»
    keep_ids: List[int] = []

    # 2.1 — сохранить меню (если есть резолвер id)
    menu_mid: Optional[int] = None
    with contextlib.suppress(Exception):
        from core.menu import get_menu_message_id  # lazy import
        menu_mid = get_menu_message_id(uid)  # type: ignore[assignment]
    if isinstance(menu_mid, int):
        keep_ids.append(menu_mid)

    # 2.2 — сохранить возможные карточки «Мои игры»
    try:
        games_bucket: Dict[int, Any] = getattr(state, "games_by_user", {}) or {}
        if isinstance(games_bucket, dict) and uid in games_bucket:
            mids = games_bucket.get(uid)
            if isinstance(mids, list):
                for _m in mids:
                    with contextlib.suppress(Exception):
                        keep_ids.append(int(_m))
            elif isinstance(mids, int):
                keep_ids.append(int(mids))
    except Exception:
        pass

    # 2.3 — запустить SSOT-вакуум с белым списком
    with contextlib.suppress(Exception):
        await vacuum_private(uid, keep=keep_ids)  # type: ignore[misc]
        logger.debug("[report:vacuum] vacuum_private(uid, keep=%s) ok uid=%s", keep_ids, uid)


# ────────────────────────────────────────────────────────────────────
# core: редактирование/отправка отчёта (с обязательной предчисткой)
# ────────────────────────────────────────────────────────────────────

async def _edit_or_send_report(uid: int, text: str, kb: InlineKeyboardMarkup) -> None:
    """
    Показ/обновление сводного отчёта в ЛС пользователя.

    Поведение:
      • Перед показом — _pre_render_vacuum(uid) (удалит только детали и старый отчёт).
      • Если отчёт уже существует — редактируем его «на месте».
      • «Message is not modified» трактуем как успех.
    """
    bot = Bot.get_current()

    # Жёсткая (но точечная) очистка перед отрисовкой дашборда отчёта
    await _pre_render_vacuum(uid)

    mid = getattr(state, "personal_report_message_id", None)
    can_edit = isinstance(mid, int) and mid > 0

    if can_edit:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=uid,
                message_id=mid,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            # Обновим last_user_messages, не задевая прочие сообщения
            try:
                bucket = getattr(state, "last_user_messages", {})
                if isinstance(bucket, dict):
                    lst = list(bucket.get(int(uid), []) or [])
                    lst = [m for m in lst if getattr(m, "message_id", None) != mid]

                    class _Msg:
                        def __init__(self, message_id: int) -> None:
                            self.message_id = message_id

                    lst.append(_Msg(mid))
                    bucket[int(uid)] = lst
                    setattr(state, "last_user_messages", bucket)
            except Exception:
                pass
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            with contextlib.suppress(Exception):
                await bot.delete_message(uid, mid)  # удалить битый отчёт
            setattr(state, "personal_report_message_id", None)
        except Exception:
            with contextlib.suppress(Exception):
                setattr(state, "personal_report_message_id", None)

    sent = await bot.send_message(
        uid, text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True
    )
    state.personal_report_message_id = sent.message_id

    # учёт в last_user_messages
    try:
        bucket = getattr(state, "last_user_messages", {})
        if isinstance(bucket, dict):
            lst = list(bucket.get(int(uid), []) or [])
            lst = [m for m in lst if getattr(m, "message_id", None) != sent.message_id]
            lst.append(sent)
            bucket[int(uid)] = lst
            setattr(state, "last_user_messages", bucket)
    except Exception:
        pass

    # мягкий контекст UI
    try:
        ui_ctx = getattr(state, "ui_context", {}) or {}
        ui_ctx[uid] = "poll_report"
        setattr(state, "ui_context", ui_ctx)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# public API: вызовы из create_poll / «📊 Отчёт по опросу» / «Назад к списку»
# ────────────────────────────────────────────────────────────────────

async def _send_leader_report(leader_id: int) -> None:
    text = await generate_poll_report()             # type: ignore[name-defined]
    kb   = _build_report_keyboard()                 # type: ignore[name-defined]
    await _edit_or_send_report(leader_id, text, kb)

async def _sync_leader_report(leader_id: Optional[int] = None) -> None:
    lid = leader_id or getattr(state, "current_poll_leader", None)
    if not lid:
        with contextlib.suppress(Exception):
            from core.db import get_all_leader_uids
            got = await get_all_leader_uids()
            lid = (got or [None])[0]
    if not lid:
        logger.debug("[sync_report] leader_id undefined — skip")
        return
    await _send_leader_report(int(lid))

async def sync_report() -> None:
    await _sync_leader_report()

@router.message(lambda m: (m.text or "") == "📊 Отчёт по опросу")  # type: ignore[name-defined]
async def poll_report_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui  = await get_user_info(uid) or {}
    if ui.get("role") not in getattr(settings, "ACCESS", {}).get("poll", ["admin", "leader"]):
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))  # type: ignore[name-defined]
        with contextlib.suppress(Exception):
            await message.delete()
        return

    if not getattr(state, "coordination_cycle_active", False):
        await message.answer("⚠️ Нет активных опросов.", reply_markup=await get_main_menu(uid))  # type: ignore[name-defined]
        with contextlib.suppress(Exception):
            await message.delete()
        return

    # Сформируем текст и клавиатуру и покажем отчёт (внутри будет pre-vacuum)
    text = await generate_poll_report()     # type: ignore[name-defined]
    kb   = _build_report_keyboard()         # type: ignore[name-defined]
    await _edit_or_send_report(uid, text, kb)

    with contextlib.suppress(Exception):
        await _refresh_menu(uid)            # type: ignore[name-defined]
    with contextlib.suppress(Exception):
        await message.delete()


# ────────────────────────────────────────────────────────────────────
# плановая чистка ЛС (бережно сохраняем меню и активные detail-блоки)
# ────────────────────────────────────────────────────────────────────

async def _vacuum_old_messages() -> None:
    """
    Плановая чистка ЛС.
    • Собираем множество uid из state.
    • Бережём главное меню и ВСЕ активные detail_blocks пользователя.
    • Удаляем прочие «хвосты» через core.utils.vacuum_private(...).
    """
    uids: Set[int] = set()
    try:
        lm: Dict[int, Any] = (getattr(state, "last_user_messages", {}) or {})
        uids.update(int(u) for u in lm.keys() if isinstance(u, int))
    except Exception:
        pass
    try:
        # поддержка обоих форматов detail_blocks: (uid, deal) и uid -> {...}
        db = getattr(state, "detail_blocks", {}) or {}
        if isinstance(db, dict):
            for k in db.keys():
                if isinstance(k, tuple) and len(k) == 2:
                    with contextlib.suppress(Exception):
                        uids.add(int(k[0]))
                elif isinstance(k, int):
                    uids.add(int(k))
    except Exception:
        pass
    try:
        mm: Dict[int, int] = (getattr(state, "menu_message_id", {}) or {})
        uids.update(int(u) for u in mm.keys() if isinstance(u, int))
    except Exception:
        pass
    try:
        ai: Dict[int, Any] = (getattr(state, "assigned_index", {}) or {})
        uids.update(int(u) for u in ai.keys() if isinstance(u, int))
    except Exception:
        pass
    try:
        gib: Dict[int, Any] = (getattr(state, "games_by_user", {}) or {})
        uids.update(int(u) for u in gib.keys() if isinstance(u, int))
    except Exception:
        pass

    try:
        from core.menu import get_menu_message_id
    except Exception:  # pragma: no cover
        get_menu_message_id = lambda _uid: None  # type: ignore

    for uid in sorted(uids):
        keep_ids: List[int] = []

        # бережём главное меню
        with contextlib.suppress(Exception):
            mid = get_menu_message_id(uid)
            if isinstance(mid, int):
                keep_ids.append(mid)

        # бережём активные detail-сообщения (оба формата)
        try:
            db = getattr(state, "detail_blocks", {}) or {}
            if isinstance(db, dict):
                # старый формат
                bucket = db.get(uid) if isinstance(db.get(uid), dict) else None  # type: ignore[index]
                if isinstance(bucket, dict):
                    for mids in bucket.values():
                        if isinstance(mids, list):
                            for it in mids:
                                with contextlib.suppress(Exception):
                                    keep_ids.append(int(it))
                        elif isinstance(mids, int):
                            keep_ids.append(int(mids))
                # SSOT-ключи
                for (k_uid, _deal), mids in list(db.items()):
                    if not (isinstance(k_uid, int) and isinstance(_deal, (int, str))):
                        continue
                    if int(k_uid) != int(uid):
                        continue
                    if isinstance(mids, list):
                        for it in mids:
                            with contextlib.suppress(Exception):
                                keep_ids.append(int(it))
                    elif isinstance(mids, int):
                        keep_ids.append(int(mids))
        except Exception:
            pass

        # запускаем SSOT-«пылесос» с белым списком
        with contextlib.suppress(Exception):
            await vacuum_private(uid, keep=keep_ids)  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────
# _test(): минимальная проверка очистки реестра (без Telegram API)
# ────────────────────────────────────────────────────────────────────

def _test() -> None:
    """
    Эмулируем SSOT-реестр state.detail_blocks[(uid, deal)] и проверяем,
    что _pre_render_vacuum очищает сообщения и ключи.
    """
    uid = 123

    # подготовим реестр деталей (SSOT)
    if not isinstance(getattr(state, "detail_blocks", None), dict):
        setattr(state, "detail_blocks", {})
    state.detail_blocks[(uid, 111)] = [1001, 1002]  # type: ignore[index]
    state.detail_blocks[(uid, 222)] = [1003]        # type: ignore[index]

    # положим «старый отчёт»
    setattr(state, "personal_report_message_id", 777)

    import asyncio

    class _DummyBot:
        async def delete_message(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            return

    # подменяем Bot.get_current() на заглушку
    orig_get = getattr(Bot, "get_current")
    setattr(Bot, "get_current", staticmethod(lambda: _DummyBot()))  # type: ignore[method-assign]

    try:
        asyncio.run(_pre_render_vacuum(uid))
        # detail-реестр по tuple-ключам очищен
        assert not any(k for k in state.detail_blocks.keys() if isinstance(k, tuple) and k[0] == uid)  # type: ignore[attr-defined]
        # отчёт обнулён
        assert getattr(state, "personal_report_message_id", None) in (None, 0)
    finally:
        setattr(Bot, "get_current", orig_get)  # type: ignore[misc]

# История изменений:
# 2025-08-28 • v0.96j — поддержан SSOT-формат state.detail_blocks[(uid, deal_id)];
#   жёсткая очистка detail_index, корректный сбор keep для планового вакуума;
#   выровнено под SSOT/фиксы Pylance.


# ════════════════════════════════════════════════════════════════════
# [1] ГЛАВНОЕ МЕНЮ (ре-экспорт)
# ════════════════════════════════════════════════════════════════════
from core.menu import get_main_menu  # noqa: F401


# ════════════════════════════════════════════════════════════════════
# [2] ХЕЛПЕРЫ: РОЛИ, СТАТУСЫ, НАПОМИНАНИЯ, DETAIL-REFRESH
# ════════════════════════════════════════════════════════════════════
_RE_NON_ALNUM = re.compile(r"[^\w\d]+", re.UNICODE)
_ADMIN_PKGS = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}

def _clean(txt: str) -> str:
    return _RE_NON_ALNUM.sub(" ", txt or "").lower().strip()

def _role_cfg(game_name: str) -> Dict[str, int]:
    """Толерантный поиск конфигурации ролей из settings.GAME_ROLE_MAPPING."""
    norm = _clean(game_name)
    best_ratio = 0.0
    best_cfg: Optional[Dict[str, int]] = None
    for key, cfg in getattr(settings, "GAME_ROLE_MAPPING", {}).items():
        k_norm = _clean(str(key))
        if norm == k_norm or norm in k_norm or k_norm in norm:
            return cfg
        ratio = SequenceMatcher(None, norm, k_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_cfg = ratio, cfg
    return best_cfg or {"main_leaders": 1, "assistants": 0}

def _deal_title(deal: Dict[str, Any]) -> str:
    return str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}")

def _need_admin_by_package(pkg_raw: str) -> int:
    return 1 if _clean(pkg_raw) in _ADMIN_PKGS else 0

def _slot_uid(val: Any) -> Optional[int]:
    """Парсит uid из значения слота: int | 'uid' | 'Имя Ф.|uid'."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        try:
            return int(s)
        except Exception:
            return None
    return None

async def _send_reminders() -> None:
    """ЛС тем, кто ещё не ответил в опросе (по get_all_leader_uids)."""
    if state.force_closed or not state.coordination_cycle_active:
        return
    try:
        all_uids = set(map(int, await get_all_leader_uids()))
    except Exception:
        return
    answered: Set[int] = set()
    for pdata in (state.responses or {}).values():
        try:
            for arr in list((pdata.get("deals") or {}).values()) + [pdata.get("not_available", []), pdata.get("admin_available", [])]:
                for u in (arr or []):
                    if isinstance(u, dict):
                        answered.add(int(u.get("user_id", 0)))
        except Exception:
            pass
    pending = all_uids - answered
    bot = Bot.get_current()
    for uid in pending:
        with contextlib.suppress(Exception):
            await bot.send_message(uid, "👋 Напоминание! Отметьтесь в опросе.")

def _cancel_reminders() -> None:
    for h in (getattr(state, "reminder_tasks", []) or []):
        with contextlib.suppress(Exception):
            h.cancel()
    state.reminder_tasks.clear()

def _schedule_reminders() -> None:
    _cancel_reminders()
    loop = asyncio.get_event_loop()
    for hours in (6, 18):
        state.reminder_tasks.append(
            loop.call_later(hours * 3600, lambda: asyncio.create_task(_send_reminders()))
        )
    logger.debug("[reminders] scheduled (6 h, 18 h)")

async def _refresh_detail_views(impacted: Set[int], refresh_all: bool = False) -> None:
    """
    Перерисовывает открытые detail-view’ы; refresh_all — перерисовать все текущие игры.
    ВАЖНО: если у пользователя активен контекст «my_games», мы НЕ трогаем его ЛС,
    чтобы не было автопереходов/мигания при работе с дашбордом «🎲 Мои игры».

    Функция вызывает handlers.poll_details.refresh_deal_details с bot=...;
    при несовпадении сигнатуры делает дауншифт до легаси позиционных аргументов.
    """
    if not callable(refresh_deal_details):
        return

    # Набор deal_id для обновления
    if refresh_all:
        impacted = {
            int(d.get("id", 0)) for d in (state.current_poll_deals or []) if int(d.get("id", 0))
        }
    impacted = {int(x) for x in (impacted or set()) if int(x)}

    detail_blocks = (getattr(state, "detail_blocks", {}) or {})
    ui_ctx = (getattr(state, "ui_context", {}) or {})
    bot = Bot.get_current()

    async def _call_refresh(u: int, d: int) -> Optional[Dict[str, Any]]:
        try:
            return await refresh_deal_details(bot=bot, uid=u, deal_id=d)  # type: ignore[call-arg]
        except TypeError:
            return await refresh_deal_details(u, d)  # type: ignore[misc]
        except Exception as e:
            logger.debug("[details] refresh failed uid=%s deal=%s: %s", u, d, e)
            return None

    tasks: List[asyncio.Task] = []
    for (uid, deal_id), _msgs in list(detail_blocks.items()):
        try:
            uid_i, did_i = int(uid), int(deal_id)
        except Exception:
            continue

        # ⛔ не трогаем «Мои игры»
        if ui_ctx.get(uid_i) == "my_games":
            logger.debug("[polls] skip details refresh for uid=%s (context=my_games)", uid_i)
            continue

        if did_i in impacted:
            tasks.append(asyncio.create_task(_call_refresh(uid_i, did_i)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

# История изменений:
# 2025-08-25 — исправлен DETAIL-REFRESH: передаём bot=..., дауншифт к легаси; устойчивый лог.
# 2025-08-14 — фильтрация по state.ui_context == 'my_games'


# ════════════════════════════════════════════════════════════════════
# [2.1] HANDLER: «Детали игры» из отчёта (show_deal_{id}) — ТИХАЯ ПЕРЕРИСОВКА
# ════════════════════════════════════════════════════════════════════

# ── [2.1.1] Импорты и lazy-imports рендеров/утилит ──────────────────

from typing import List
import contextlib
import logging
import re

from aiogram import F, Bot, Router, types  # ← ВАЖНО: F из aiogram, не из aiogram.filters!
from core.state import state

logger = logging.getLogger(__name__)

# Рендеры из handlers.poll_details (основной путь)
try:
    from handlers.poll_details import (
        render_detail as _render_detail_public,      # async def render_detail(bot=..., uid=..., deal_id=..., force_approved=False)
        refresh_deal_details as _refresh_detail,     # async def refresh_deal_details(bot=..., uid=..., deal_id=..., force_approved=False)
    )
except Exception:  # pragma: no cover
    _render_detail_public = None  # type: ignore[assignment]
    _refresh_detail = None        # type: ignore[assignment]

# Доступ к реестру «детальных» сообщений (может отсутствовать в старых сборках)
try:
    from handlers.poll_details import (
        _get_block as _pd_get_block,     # def _get_block(uid:int, deal_id:int) -> List[types.Message]
        _detach_from_last_user_messages as _pd_detach,  # def _detach_from_last_user_messages(uid, keep)
    )
except Exception:  # pragma: no cover
    _pd_get_block = None  # type: ignore[assignment]
    _pd_detach = None     # type: ignore[assignment]

# SSOT-вакуум для точечной чистки дублей
try:
    from core.utils import vacuum_private as _vacuum_private  # async def vacuum_private(uid:int, keep:List[int]|None=None)
except Exception:  # pragma: no cover
    _vacuum_private = None  # type: ignore[assignment]

# router объявлен выше в модуле; эта аннотация помогает Pylance
router: Router  # noqa: F401


# ── [2.1.2] Вспомогательные функции доступа к блоку сообщений ───────
def _safe_get_block(uid: int, deal_id: int) -> List[types.Message]:
    if callable(_pd_get_block):
        try:
            return _pd_get_block(uid, deal_id)  # type: ignore[misc]
        except Exception:
            return []
    return []

def _msg_ids(msgs: List[types.Message]) -> List[int]:
    out: List[int] = []
    for m in msgs:
        mid = getattr(m, "message_id", None)
        if isinstance(mid, int):
            out.append(mid)
    return out


# ── [2.1.3] Унифицированный вызов рендера (приоритет тихой обёртки) ─
async def _do_detail_render(bot: Bot, uid: int, deal_id: int) -> None:
    """
    Идём через публичную обёртку render_detail(...): она сама решит, делать refresh
    существующих сообщений или первичный рендер. Фолбэк — прямой refresh_deal_details(...).
    """
    if callable(_render_detail_public):
        await _render_detail_public(bot=bot, uid=uid, deal_id=deal_id, force_approved=False)  # type: ignore[misc]
        return
    if callable(_refresh_detail):
        await _refresh_detail(bot=bot, uid=uid, deal_id=deal_id, force_approved=False)  # type: ignore[misc]
        return
    raise RuntimeError("poll_details.render_detail / refresh_deal_details not available")


# ── [2.1.4] Пост-гард: удаляем старую пачку, если рендер напечатал новую ─
async def _post_guard(uid: int, deal_id: int, before: List[types.Message], after: List[types.Message]) -> None:
    """
    Если после рендера message_id не совпадают с прежними — считаем, что произошла
    деградация в «новую печать». Подчищаем «до», оставляем «после».
    """
    try:
        if not before or not after or not callable(_vacuum_private):
            return
        b_ids = set(_msg_ids(before))
        a_ids = set(_msg_ids(after))
        if b_ids and a_ids and b_ids.isdisjoint(a_ids):
            with contextlib.suppress(Exception):
                await _vacuum_private(uid, keep=list(a_ids))  # type: ignore[misc]
            if callable(_pd_detach):
                with contextlib.suppress(Exception):
                    _pd_detach(uid, after)  # type: ignore[misc]
    except Exception as e:  # pragma: no cover
        logger.warning("[details] post-guard failed uid=%s deal_id=%s: %s", uid, deal_id, e)


# ── [2.1.5] Хендлер кнопки «show_deal_{id}» — тихий показ/перерисовка ─
@router.callback_query(F.data.startswith("show_deal_"))
async def _cb_open_detail_from_report(callback: types.CallbackQuery) -> None:
    """
    Открывает/перерисовывает блок деталей игры из отчёта.
    Приоритет — тихая замена. При деградации — страховка от дублей.
    """
    # Быстрый ACK
    with contextlib.suppress(Exception):
        await callback.answer()

    m = re.search(r"show_deal_(\d+)$", str(callback.data or ""))
    if not m:
        with contextlib.suppress(Exception):
            await callback.answer("⚠️ Ошибочная кнопка.", show_alert=True)
        return

    deal_id = int(m.group(1))
    uid = int(callback.from_user.id or 0)
    bot: Bot = callback.message.bot if callback.message else Bot.get_current()

    before_msgs = _safe_get_block(uid, deal_id)

    try:
        await _do_detail_render(bot=bot, uid=uid, deal_id=deal_id)
    except Exception as e:
        logger.exception("[details] render failed uid=%s deal_id=%s: %s", uid, deal_id, e)
        with contextlib.suppress(Exception):
            await callback.answer("Ошибка отрисовки деталей.", show_alert=True)
        return

    after_msgs = _safe_get_block(uid, deal_id)
    await _post_guard(uid=uid, deal_id=deal_id, before=before_msgs, after=after_msgs)

    # Помечаем UI-контекст, чтобы внешние авто-апдейты не мешали блоку деталей
    try:
        (getattr(state, "ui_context", {}) or {}).update({uid: "poll_details"})
    except Exception:
        pass


# ── [2.1.6] Мини-тест ───────────────────────────────────────────────
async def _test__details_sanity() -> None:
    assert callable(_do_detail_render)
    assert callable(_msg_ids)
    logger.debug("[tests] [2.1] smoke-ok")

# История изменений [2.1]:
# • 2025-08-25 — фикс импорта F (aiogram.F), полная проверка «тихой замены»,
#                пост-гард через vacuum_private; выровнено под SSOT/фиксы Pylance.



# ────────────────────────────────────────────────────────────────────
# [2.4] Светофор-адаптер (совместимость sync/async, безопасные ошибки)
# ────────────────────────────────────────────────────────────────────
async def _sv_status(user_id: int, game_name: str) -> str:
    """
    Универсальный вызов статуса «Светофора».
    • Поддерживает sync/async реализацию services.gsheets.get_user_status_from_svetofor.
    • На ошибках возвращает ''.
    """
    try:
        res = get_user_status_from_svetofor(user_id, game_name)
        status = await res if asyncio.iscoroutine(res) else res  # type: ignore
        status = (status or "").strip().lower()
        return status if status in {"green", "yellow", "red", ""} else ""
    except Exception as exc:
        logger.debug("[svetofor] fail uid=%s game=%s: %s", user_id, game_name, exc)
        return ""

# ────────────────────────────────────────────────────────────────────
# [2.5] АВТОРАСПРЕДЕЛЕНИЕ ИЗ ОТВЕТОВ ОПРОСА → state.distribution_cache
# (v2: бережное добирание пустых слотов; ручные перестановки сохраняем)
# ────────────────────────────────────────────────────────────────────
async def _auto_assign_from_responses(impacted: Set[int], apply_to_all_on_admin_flag: bool = False) -> None:
    """
    Пересчитывает предварительное распределение, НЕ затирая ручные перестановки.

    Логика:
      1) Собираем пул откликнувшихся на сделку (raw_pool) и статусы «Светофора».
      2) Берём предыдущий dist из state.distribution_cache[str(did)] как БАЗУ.
      3) Чистим из базы только тех, КОГО НЕТ в raw_pool (пользователь снял отклик),
         и устраняем дубли (инвариант 1 пользователь = 1 роль) в порядке: lead → assist → admin.
         «trainee» не влияет на готовность и сохраняется как есть.
      4) Добираем ТОЛЬКО ПУСТЫЕ слоты по приоритетам:
         Светофор (green < yellow < red/нет) → меньше игр за месяц → uid.
         Для core-ролей (lead/assist) red не берём автоматически.
         Для admin — только те, кто нажал «могу админом».
      5) Сохраняем результат обратно в distribution_cache[str(did)].

    apply_to_all_on_admin_flag: True → пересчитать все текущие игры цикла.
    """
    # Если отметили/сняли «Админом» — пересчитываем все игры текущего цикла
    if apply_to_all_on_admin_flag:
        impacted = {int(d.get("id", 0)) for d in (state.current_poll_deals or []) if int(d.get("id", 0))}
    if not impacted:
        return

    # rank для светофора
    def _rank(status: str) -> int:
        s = (status or "").lower()
        return 0 if s == "green" else 1 if s == "yellow" else 2 if s == "red" else 3

    # Метка слота «Имя Ф.|uid»
    def _fmt(info: Dict[str, Any]) -> str:
        fn = (info.get("first_name") or "").strip()
        li = (info.get("last_name_initial") or "").strip()
        uid = int(info.get("uid") or info.get("user_id") or 0)
        base = (f"{fn} {li}." if li else fn).strip() or f"user{uid}"
        return f"{base}|{uid}"

    monthly_counters = await _get_monthly_counters()

    # Быстрый доступ к сделкам по id
    deals_by_id: Dict[int, Dict[str, Any]] = {int(d.get("id", 0)): d for d in (state.current_poll_deals or [])}

    # Пул «могу админом» (по всем ответам цикла)
    admin_pool_global: Dict[int, Dict[str, Any]] = {}
    for pdata in (state.responses or {}).values():
        for adm in (pdata.get("admin_available") or []):
            uid = int(adm.get("user_id", 0))
            if uid:
                admin_pool_global[uid] = {
                    "uid": uid,
                    "first_name": adm.get("first_name", ""),
                    "last_name_initial": adm.get("last_name_initial", ""),
                    "is_admin_eligible": True,
                }

    for did in {int(x) for x in impacted if int(x)}:
        deal = deals_by_id.get(did)
        if not deal:
            continue

        game_name = str(deal.get("game_name") or deal.get("name") or "")
        package   = str(deal.get("package") or "")
        need      = _role_cfg(game_name)
        need_main = int(need.get("main_leaders", 1))
        need_ass  = int(need.get("assistants", 0))
        need_adm  = _need_admin_by_package(package)

        # 1) Пул отметившихся за ЭТУ сделку
        raw_pool: Dict[int, Dict[str, Any]] = {}
        for pdata in (state.responses or {}).values():
            deals_map = (pdata.get("deals") or {})
            if did in deals_map:
                for u in (deals_map[did] or []):
                    uid = int(u.get("user_id", 0))
                    if uid:
                        raw_pool[uid] = {
                            "uid": uid,
                            "first_name": u.get("first_name", ""),
                            "last_name_initial": u.get("last_name_initial", ""),
                            "is_admin_eligible": False,
                        }

        # Обновим признак «могу админом» из глобального пула
        for uid in list(raw_pool.keys()):
            if uid in admin_pool_global:
                raw_pool[uid]["is_admin_eligible"] = True

        uids = list(raw_pool.keys())

        # 2) Статусы «Светофора» параллельно
        async def _one(uid_: int) -> Tuple[int, str]:
            return uid_, await _sv_status(uid_, game_name)

        sv_pairs = await asyncio.gather(*[_one(u) for u in uids], return_exceptions=True)
        sv: Dict[int, str] = {}
        for p in sv_pairs:
            if isinstance(p, Exception):
                continue
            uid_, st = p
            sv[uid_] = (st or "")

        # 3) Подготовим отсортированные пулы для автодобора
        def _key(info: Dict[str, Any]) -> Tuple[int, int, int]:
            uid_i = int(info.get("uid") or 0)
            return _rank(sv.get(uid_i, "")), int(monthly_counters.get(uid_i, 0)), uid_i

        pool_main = [raw_pool[u] for u in uids if _rank(sv.get(u, "")) == 0]
        pool_ass  = [raw_pool[u] for u in uids if _rank(sv.get(u, "")) in (0, 1)]
        pool_adm  = [raw_pool[u] for u in uids if raw_pool[u].get("is_admin_eligible")]

        pool_main.sort(key=_key)
        pool_ass .sort(key=_key)
        pool_adm .sort(key=_key)

        # 4) БАЗА: берём существующий dist и аккуратно очищаем из него только «снявшихся»
        if not getattr(state, "distribution_cache", None):
            state.distribution_cache = {}

        base: Dict[str, Any] = dict((state.distribution_cache or {}).get(str(did)) or {})

        # Гарантируем наличие требуемых ключей
        need_main, need_ass, _need_admin = _ensure_role_slots(base, game_name, package)

        # Порядок для устранения дублей: lead → assist → admin (trainee не трогаем)
        order_slots: List[str] = []
        order_slots += [f"lead{i}" for i in range(1, max(1, need_main) + 1)]
        order_slots += [f"assistant{i}" for i in range(1, max(0, need_ass) + 1)]
        order_slots += ["admin"]

        # used — у кого уже есть роль в БАЗЕ (после чистки) 
        used: Set[int] = set()
        dist: Dict[str, Any] = dict(base)  # начнём с копии; trainee сохраняется автоматически

        # Очистка: сносим из lead/assist/admin всех, кого нет в raw_pool, и убираем дубли
        for key in order_slots:
            cur = dist.get(key)
            uid = _slot_uid(cur)
            if not uid:
                dist[key] = None
                continue
            if uid not in raw_pool:
                # пользователь снял отклик → убрать из состава
                dist[key] = None
                continue
            if uid in used:
                # инвариант «1 пользователь = 1 роль»
                dist[key] = None
                continue
            used.add(uid)

        # 5) Добор пустых слотов из пулов с приоритетами (ручные назначения не трогаем)
        def _take_from_pool(pool: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            for p in pool:
                uid_p = int(p.get("uid") or 0)
                if uid_p and uid_p not in used:
                    return p
            return None

        # Основные ведущие
        for i in range(1, max(1, need_main) + 1):
            k = f"lead{i}"
            if dist.get(k) in (None, "", 0):
                pick = _take_from_pool(pool_main)
                if pick:
                    dist[k] = _fmt(pick)
                    used.add(int(pick["uid"]))

        # Помощники
        for i in range(1, max(0, need_ass) + 1):
            k = f"assistant{i}"
            if dist.get(k) in (None, "", 0):
                pick = _take_from_pool(pool_ass)
                if pick:
                    dist[k] = _fmt(pick)
                    used.add(int(pick["uid"]))

        # Админ (если требуется пакетом)
        if need_adm:
            if dist.get("admin") in (None, "", 0):
                pick = _take_from_pool(pool_adm)  # ← строго из тех, кто нажал «могу админом»
                if pick:
                    dist["admin"] = _fmt(pick)
                    used.add(int(pick["uid"]))
        else:
            # если пакетом не требуется — слот не обязательный; но не чистим, если там вручную поставили
            dist.setdefault("admin", None)

        # 6) Сохранение результата
        state.distribution_cache[str(did)] = dist

    # История изменений:
    # 2025-08-26 — v2: «бережное автораспределение» — сохраняем ручные перестановки, чистим только снятых,
    #                   добор только пустых слотов; устранение дублей lead→assist→admin; SSOT/инварианты сохранены.
    # 2025-09-02 — правка: админ только из pool_adm (без фолбэка на ассист).


# ────────────────────────────────────────────────────────────────────
# [2.5.1] ПРИОРИТЕТЫ ПО ТЕГАМ (AmoCRM) — кэш месячных счётчиков
# ────────────────────────────────────────────────────────────────────
async def _load_monthly_role_counters(force: bool = False) -> Dict[int, int]:
    """
    Загружает количество сыгранных игр за прошедший месяц по подтверждающим тегам AmoCRM.
    Источник данных — сервисный слой services.amocrm (SSOT).
    Результат кэшируется в state.monthly_role_counters как {uid: count}.

    • Если сервисный хелпер отсутствует/не доступен — возвращает пустой словарь.
    • Безопасен для ранних сборок: импорт внутри, ошибки заглушаются.
    """
    if not force:
        cached = getattr(state, "monthly_role_counters", None)
        if isinstance(cached, dict):
            # приведём к int → int на всякий случай
            try:
                return {int(k): int(v) for k, v in cached.items()}
            except Exception:
                pass

    # Локальный импорт — чтобы избежать циклических зависимостей на старых сборках
    try:
        from services.amocrm import get_monthly_role_tag_counters  # type: ignore
    except Exception:
        async def get_monthly_role_tag_counters(*_: Any, **__: Any) -> Dict[int, int]:  # type: ignore
            return {}

    try:
        res = get_monthly_role_tag_counters()
        data = await res if asyncio.iscoroutine(res) else res  # type: ignore
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:  # сетевые/сервисные ошибки не должны ронять цикл
        logger.debug("[priority] monthly counters load failed: %s", exc)
        data = {}

    # Нормализуем и кладём в state
    try:
        normalized = {int(k): int(v) for k, v in (data or {}).items()}
    except Exception:
        normalized = {}
    setattr(state, "monthly_role_counters", normalized)
    return normalized


async def _get_monthly_counters() -> Dict[int, int]:
    """
    Возвращает кэш месячных счётчиков {uid: count}. Если кэша нет — грузит.
    Используется при автораспределении для приоритизации (меньше игр → выше приоритет).
    """
    cached = getattr(state, "monthly_role_counters", None)
    if isinstance(cached, dict):
        try:
            return {int(k): int(v) for k, v in cached.items()}
        except Exception:
            pass
    return await _load_monthly_role_counters(force=False)

# История изменений:
# 2025-08-24 — новый блок: кэш месячных счётчиков из AmoCRM (выровнено под SSOT, безопасные импорты).

# ════════════════════════════════════════════════════════════════════
# [2.6] CHAT RESOLVER (куда слать сервисные уведомления)
# ════════════════════════════════════════════════════════════════════
# Переведено на SSOT: используем общий резолвер из core.utils.
from core.utils import resolve_notify_chat_id

# История изменений:
# 2025-08-18 — выровнено под SSOT: локальный _resolve_notify_chat_id удалён, используем core.utils.resolve_notify_chat_id.


# ███ [2.7] SWAP / DISTRIBUTION HELPERS
# --------------------------------------------------------------------
from core.utils import short_name, team_bulleted_lines, assigned_role_from_state as _assigned_role_from_state
from services.amocrm import get_amocrm_deals  # для снапшота сделки (fallback)
from typing import Tuple, Optional, Dict, Any, List, Set

def _slot_label(uid: int, base: Optional[str] = None) -> str:
    """Метка слота: 'Имя Ф.|uid'."""
    return f"{(base or '').strip() or 'user'+str(uid)}|{uid}"

def _remove_uid_from_dist(dist: Dict[str, Any], uid: int) -> None:
    """Инвариант «1 пользователь = 1 роль» — убираем uid из всех lead*/assistant*/admin/trainee."""
    for k, v in list(dist.items()):
        if not isinstance(k, str):
            continue
        if k.startswith("lead") or k.startswith("assistant") or k == "admin":
            if isinstance(v, str) and v.rsplit("|", 1)[-1].isdigit() and int(v.rsplit("|", 1)[-1]) == uid:
                dist[k] = None
        if k == "trainee":
            # trainee хранится как список строк меток
            if isinstance(v, list):
                dist[k] = [t for t in v if not (isinstance(t, str) and t.rsplit("|", 1)[-1].isdigit()
                                                and int(t.rsplit("|", 1)[-1]) == uid)]

def _ensure_role_slots(dist: Dict[str, Any], game_name: str, package: str) -> Tuple[int, int, int]:
    """Создаёт недостающие ключи lead{i}/assistant{i}/admin согласно конфигурации. Стажёры — список."""
    need = _role_cfg(game_name)
    need_main = int(need.get("main_leaders", 1))
    need_assist = int(need.get("assistants", 0))
    need_admin = _need_admin_by_package(package)
    for i in range(1, max(1, need_main) + 1):
        dist.setdefault(f"lead{i}", None)
    for i in range(1, max(0, need_assist) + 1):
        dist.setdefault(f"assistant{i}", None)
    dist.setdefault("admin", None if need_admin else None)
    # стажёры — всегда список (не влияют на готовность)
    dist.setdefault("trainee", [])
    if not isinstance(dist["trainee"], list):
        dist["trainee"] = []  # приведение типов
    return need_main, need_assist, need_admin

def _first_empty_slot(dist: Dict[str, Any], prefix: str, count: int) -> Optional[str]:
    """Возвращает имя первого пустого слота с данным префиксом (lead/assistant). Если нет свободных — None."""
    for i in range(1, count + 1):
        key = f"{prefix}{i}"
        if dist.get(key) in (None, "", 0):
            return key
    return None  # раньше возвращали prefix1 → это могло перезаписывать занятый слот

async def _insert_candidate_into_distribution(deal_id: int, role: str, uid: int, status: str) -> Dict[str, Any]:
    """
    Точечно встраивает кандидата в distribution_cache[str(deal_id)]:
    • green/yellow → целевая роль, но только в ПУСТОЙ слот;
    • red → добавляем в trainee (список меток), готовность не увеличивает;
    • НИКОГДА не перезаписывает занятые core-слоты (no-op, если свободных нет).
    Инвариант: 1 пользователь = 1 роль.
    Возвращает актуализированный dist.
    """
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == int(deal_id)), {}) or {}
    game_name = str(deal.get("game_name") or deal.get("name") or "")
    package = str(deal.get("package") or "")
    if not getattr(state, "distribution_cache", None):
        state.distribution_cache = {}
    dist: Dict[str, Any] = dict((state.distribution_cache or {}).get(str(deal_id)) or {})
    need_main, need_assist, _need_admin = _ensure_role_slots(dist, game_name, package)

    # удалить кандидата из всех ролей (если вдруг уже был)
    _remove_uid_from_dist(dist, uid)

    label = _slot_label(uid, await short_name(uid))

    if status == "red" and role in {"main", "assist"}:
        # «красный» — добавляем в стажёры, без дублей
        t = dist.get("trainee")
        if isinstance(t, list):
            if label not in t:
                t.append(label)
            dist["trainee"] = t
        else:
            dist["trainee"] = [label]
    else:
        if role == "main":
            key = _first_empty_slot(dist, "lead", need_main)
            if key:
                dist[key] = label  # только если есть свободное место
        elif role == "assist":
            key = _first_empty_slot(dist, "assistant", need_assist)
            if key:
                dist[key] = label
        elif role == "admin":
            # админ — один слот, но НЕ переписываем, если уже занят
            if dist.get("admin") in (None, "", 0):
                dist["admin"] = label
        else:
            # защитный фолбэк — пытаемся как помощник, но без перезаписи занятых
            key = _first_empty_slot(dist, "assistant", max(1, need_assist))
            if key:
                dist[key] = label

    state.distribution_cache[str(deal_id)] = dist
    return dist

async def _find_deal_snapshot(deal_id: int) -> Dict[str, Any]:
    """
    Находит сделку сперва в state.current_poll_deals, при отсутствии — в AmoCRM.
    Возвращает dict (может быть пустым).
    """
    did = int(deal_id)
    for d in (getattr(state, "current_poll_deals", []) or []):
        try:
            if int(d.get("id", 0)) == did:
                return d
        except Exception:
            continue
    try:
        deals = await get_amocrm_deals()
        for d in deals or []:
            if int(d.get("id", 0)) == did:
                return d
    except Exception:
        pass
    return {}

# История изменений:
# 2025-08-27 — trainee как список (мульти-стажёры); _first_empty_slot больше не возвращает принудительный prefix1;
#              _insert_candidate... не перезаписывает занятые core-слоты (no-op), строгое соблюдение инвариантов SSOT.
# ────────────────────────────────────────────────────────────────────
# [2.8] FILTER HELPERS: статусы/окно дат/теги ведущих/«предварительно»
# ────────────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r".+\.(?:1|2|Адм|Стаж)$", re.IGNORECASE)

def _window_days() -> int:
    try:
        d = int(getattr(settings, "POLL_WINDOW_DAYS", 0) or 10)
        return d if d > 0 else 10
    except Exception:
        return 10

def _status_ids_for_new_games() -> Set[Any]:
    """
    Набор статус_id для отбора игр в опрос:
    • settings.BRON_STATUS_ID
    • settings.PRE_APPLICATION_STATUS_ID
    • settings.NEW_GAMES_STATUS_IDS (массив)
    """
    out: Set[Any] = set()
    for k in ("BRON_STATUS_ID", "PRE_APPLICATION_STATUS_ID"):
        v = getattr(settings, k, None)
        if v is not None:
            out.add(v)
    try:
        arr = getattr(settings, "NEW_GAMES_STATUS_IDS", []) or []
        for it in arr:
            out.add(it)
    except Exception:
        pass
    return out

def _event_dt(d: Dict[str, Any]) -> Optional[datetime]:
    dt = d.get("event_datetime")
    return dt if isinstance(dt, datetime) else None

def _deal_in_window(d: Dict[str, Any], now: datetime, window: datetime) -> bool:
    dt = _event_dt(d)
    return bool(dt and now <= dt <= window)

def _has_leader_tags(d: Dict[str, Any]) -> bool:
    """
    Возвращает True, если среди тегов сделки есть тег ведущего:
    «Имя Ф.1/2/Адм/Стаж». Поддерживает list[str] и list[dict{name:...}].
    """
    tags = d.get("tags") or []
    names: List[str] = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                names.append(t)
            elif isinstance(t, dict) and "name" in t:
                names.append(str(t["name"]))
    for name in names:
        if _TAG_RE.match(name.strip()):
            return True
    return False

def _is_preliminary_status(d: Dict[str, Any]) -> bool:
    """
    Определяем «Предварительная заявка»:
    • точным равенством status_id к settings.PRE_APPLICATION_STATUS_ID (если задан),
    • либо по эвристике имени статуса (contains 'предвар').
    """
    try:
        pre_id = getattr(settings, "PRE_APPLICATION_STATUS_ID", None)
        if pre_id is not None and d.get("status_id") == pre_id:
            return True
    except Exception:
        pass
    name = str(d.get("status_name") or d.get("pipeline_status_name") or "").lower()
    return "предвар" in name

# ════════════════════════════════════════════════════════════════════
# [3] СОЗДАНИЕ ОПРОСА
# ════════════════════════════════════════════════════════════════════
@router.message(Command("create_poll"))
@router.message(lambda m: (m.text or "") == "📋 Создать опрос")
async def create_poll_handler(message: types.Message) -> None:
    uid = message.from_user.id
    logger.info("[create_poll] invoked by %d", uid)

    bot = Bot.get_current()

    # вспомогательный шорткат: показать короткое ЛС-сообщение как «экран»
    async def _screen(text: str, *, kb: Optional[InlineKeyboardMarkup] = None) -> None:
        # перед каждым экраном — жёсткий пылесос ЛС (только приват)
        with contextlib.suppress(TypeError):
            await delete_previous_private_messages(bot, uid, keep=[])
        with contextlib.suppress(Exception):
            await delete_previous_private_messages(uid, keep=[])
        # отправляем и фиксируем как текущий экран
        sent = await bot.send_message(uid, text, reply_markup=kb)
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [sent]

    # 1) доступ
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in getattr(settings, "ACCESS", {}).get("poll", ["admin", "leader"]):
        await _screen("⛔ Нет доступа.", kb=await get_main_menu(uid))
        with contextlib.suppress(Exception):
            await message.delete()
        return

    # активный цикл → просто «уже есть»
    if state.coordination_cycle_active:
        await _screen("⚠️ Уже есть активный опрос.", kb=await get_main_menu(uid))
        with contextlib.suppress(Exception):
            await _refresh_menu(uid)
        with contextlib.suppress(Exception):
            await message.delete()
        return

    # Куда публиковать опросы — единый резолвер общего чата (SSOT)
    from core.utils import resolve_notify_chat_id  # локальный импорт
    chat_id = resolve_notify_chat_id(bot)
    if not chat_id:
        await _screen("⚠️ Чат не настроен.", kb=await get_main_menu(uid))
        with contextlib.suppress(Exception):
            await message.delete()
        return
    state.admin_chat_id = chat_id  # сохранить

    # 2) загрузка сделок
    try:
        deals = await get_amocrm_deals()
    except Exception as e:
        logger.exception("[create_poll] get_amocrm_deals failed: %s", e)
        await _screen("⚠️ Не удалось получить игры из AmoCRM.")
        with contextlib.suppress(Exception):
            await message.delete()
        return

    # --- SSOT-фильтры ------------------------------------------------
    # • статус = BRON_STATUS_ID или PREBOOK_STATUS_ID (бэкомпат: PRE_APPLICATION_STATUS_ID);
    # • НЕТ ролевых тегов ведущих в AmoCRM (анализируем d['tags'], не team_leads);
    # • event_datetime ∈ [сейчас; +10 дней].
    now = datetime.now(tz=MSK_TZ)
    window = now + timedelta(days=10)  # требование: строго ближайшие 10 дней

    # Собираем допустимые status_id из настроек (без расширений)
    def _allowed_statuses() -> List[Any]:
        vals: List[Any] = []
        for attr in ("BRON_STATUS_ID", "PREBOOK_STATUS_ID", "PRE_APPLICATION_STATUS_ID"):  # PREBOOK ~ PRE_APPLICATION
            if hasattr(settings, attr):
                v = getattr(settings, attr)
                if v is not None:
                    vals.append(v)
        return vals

    ALLOWED = _allowed_statuses()

    def _status_matches(sid: Any) -> bool:
        """Сопоставление с допуском разных типов (int/str) без падений."""
        for a in ALLOWED:
            try:
                # прямое сравнение
                if sid == a:
                    return True
                # мягкое приведение к int, если возможно
                return int(sid) == int(a)  # noqa: PLW2901 (одно сравнение достаточно)
            except Exception:
                continue
        return False

    raw_deals: List[Dict[str, Any]] = []
    for d in (deals or []):
        try:
            # 1) статус — ровно BRON/PREBOOK (или PRE_APPLICATION как алиас)
            if not _status_matches(d.get("status_id")):
                continue
            # 2) дата события из кастомного поля AmoCRM
            dt = d.get("event_datetime")
            if not (isinstance(dt, datetime) and now <= dt <= window):
                continue
            # 3) никаких ролевых тегов в AmoCRM ('.1/.2/.Адм/.Стаж')
            if _has_leader_tags(d):
                continue
            raw_deals.append(d)
        except Exception:
            continue

    if not raw_deals:
        await _screen("😔 Нет подходящих игр на ближайшие 10 дней.", kb=await get_main_menu(uid))
        with contextlib.suppress(Exception):
            await message.delete()
        return

    # 3) инициализация state
    state.current_poll_deals         = raw_deals
    state.current_poll_leader        = uid
    state.responses.clear()
    state.distribution_cache.clear()
    state.poll_distribution.clear()
    state.deal_force_closed.clear()
    if hasattr(state, "confirmed_users"):  # совместимость
        state.confirmed_users.clear()
    state.current_deal_ready.clear()
    state.all_ready_notified         = False
    state.personal_report_message_id = None
    state.coordination_cycle_active  = True
    state.force_closed               = False
    state.pending_new_deals          = []  # обнуляем буфер «новых игр»

    with contextlib.suppress(Exception):
        await _refresh_menu(uid)

    # 4) публикация опросов (в рабочем чате)
    def _deal_dt_parts(d: Dict[str, Any]) -> Tuple[str, str]:
        dt = d.get("event_datetime")
        date_s = dt.strftime("%d.%m") if isinstance(dt, datetime) else str(d.get("event_date") or "—")
        et = str(d.get("event_time") or "").strip()
        time_s = et if et else (dt.strftime("%H:%M") if isinstance(dt, datetime) else str(d.get("event_time") or "—"))
        return date_s, time_s

    def _deal_extras(d: Dict[str, Any]) -> Tuple[str, str]:
        pkg = str(d.get("package") or "").strip()
        bonuses = d.get("bonuses", d.get("bonus", d.get("extra_bonuses", d.get("extra_services"))))
        return pkg, str(bonuses or "").strip()

    def _title(d: Dict[str, Any]) -> str:
        base = str(d.get("game_name") or d.get("name") or f"Сделка #{d.get('id')}")
        return f"{base} (предварительно)" if _is_preliminary_status(d) else base

    def _is_embedded(d: Dict[str, Any]) -> bool:
        src = str(d.get("source") or "").strip().lower()
        return bool(d.get("embedded") or d.get("is_embedded") or src in {"embedded", "inline", "internal"})

    # Разделяем «встроенные» и обычные: встроенные публикуем отдельными постами
    embedded = [d for d in raw_deals if _is_embedded(d)]
    regular  = [d for d in raw_deals if not _is_embedded(d)]

    urgent = any((d.get("event_datetime") or now) <= now + timedelta(days=3) for d in raw_deals)
    header_base = "🚨 Срочные!" if urgent else "📊 Новые игры"

    # общий план постов: каждый embedded — отдельный «чанк», обычные — пачками по 8
    chunks: List[List[Dict[str, Any]]] = [[d] for d in embedded]
    chunks += [regular[i:i + 8] for i in range(0, len(regular), 8)] or []

    async def _post_chunk(idx: int, total: int, chunk: List[Dict[str, Any]]) -> None:
        header = f"{header_base} (Часть {idx})" if total > 1 else header_base
        opts: List[str] = []
        idx_map: Dict[int, int] = {}

        for i, d in enumerate(chunk):
            title = _title(d)
            date_s, time_s = _deal_dt_parts(d)
            pkg_s, bonus_s = _deal_extras(d)
            parts: List[str] = [f"🎉 {title} — {date_s} {time_s}"]
            if pkg_s:
                parts.append(pkg_s)
            if bonus_s:
                parts.append(bonus_s)
            opts.append(truncate(" ".join(parts)))
            idx_map[i] = int(d.get("id", 0))

        # служебные опции (обязательны!)
        opts += ["🚫 Не смогу работать", "🛡️ Могу Администратором"]

        poll = await bot.send_poll(
            chat_id,
            header,
            opts,
            is_anonymous=False,
            allows_multiple_answers=True,
        )
        state.responses[poll.poll.id] = {
            "deals": {int(d.get("id", 0)): [] for d in chunk},
            "not_available": [],
            "admin_available": [],
            "deal_indices": idx_map,
        }
        logger.debug("[create_poll] poll sent: id=%s, deals=%s", poll.poll.id, list(idx_map.values()))

    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        if not chunk:
            continue
        await _post_chunk(idx, total_chunks, chunk)

    # 5) финальный экран в ЛС лидера: «Опросы отправлены»
    await _screen("✅ Опросы отправлены.")
    with contextlib.suppress(Exception):
        await _refresh_menu(uid)
    with contextlib.suppress(Exception):
        await message.delete()


# ════════════════════════════════════════════════════════════════════
# [3.1] «Новая игра» в активном цикле → отдельный опрос «Срочные игры»
# ════════════════════════════════════════════════════════════════════

async def _find_pending_new_deals() -> List[Dict[str, Any]]:
    """
    Ищем новые подходящие сделки в окне дат, которых ещё нет в state.current_poll_deals.
    Учитываем статусы Бронь/Предварительная и отсутствие тегов ведущих.
    """
    if not state.coordination_cycle_active:
        return []

    try:
        deals = await get_amocrm_deals()
    except Exception as e:
        logger.debug("[new_game] get_amocrm_deals failed: %s", e)
        return []

    now = datetime.now(tz=MSK_TZ)
    window = now + timedelta(days=_window_days())
    valid_statuses = _status_ids_for_new_games()
    existing = {int(d.get("id", 0)) for d in (state.current_poll_deals or [])}
    out: List[Dict[str, Any]] = []
    for d in (deals or []):
        try:
            did = int(d.get("id", 0))
            if not did or did in existing:
                continue
            if valid_statuses and d.get("status_id") not in valid_statuses:
                continue
            if not _deal_in_window(d, now, window):
                continue
            if _has_leader_tags(d):
                continue
            out.append(d)
        except Exception:
            continue
    return out

@router.callback_query(lambda c: c.data == "poll_new_game")
async def poll_new_game_handler(callback: types.CallbackQuery) -> None:
    """
    Публикует отдельный опрос «🚨 Срочные игры» по накопленным pending_new_deals.
    После публикации пополняет state.current_poll_deals и очищает pending_new_deals.
    """
    with contextlib.suppress(Exception):
        await callback.answer()

    bot = Bot.get_current()
    chat_id = state.admin_chat_id or (resolve_notify_chat_id(bot) if callable(resolve_notify_chat_id) else None)  # type: ignore[arg-type]
    if not chat_id:
        with contextlib.suppress(Exception):
            await callback.answer("⚠️ Чат не настроен.", show_alert=True)
        return

    # Обновим буфер перед отправкой (на случай, если он пуст, но уже появились новые)
    try:
        fresh = await _find_pending_new_deals()
    except Exception:
        fresh = []
    state.pending_new_deals = list(fresh or [])

    if not state.pending_new_deals:
        with contextlib.suppress(Exception):
            await callback.answer("Новых игр нет.", show_alert=True)
        return

    deals_chunk = state.pending_new_deals[:8]  # на всякий случай ограничим одним сообщением
    opts: List[str] = []
    idx_map: Dict[int, int] = {}

    def _title(d: Dict[str, Any]) -> str:
        base = str(d.get("game_name") or d.get("name") or f"Сделка #{d.get('id')}")
        return f"{base} (предварительно)" if _is_preliminary_status(d) else base

    def _deal_dt_parts(d: Dict[str, Any]) -> Tuple[str, str]:
        dt = d.get("event_datetime")
        date_s = dt.strftime("%d.%m") if isinstance(dt, datetime) else str(d.get("event_date") or "—")
        et = str(d.get("event_time") or "").strip()
        time_s = et if et else (dt.strftime("%H:%M") if isinstance(dt, datetime) else str(d.get("event_time") or "—"))
        return date_s, time_s

    def _deal_extras(d: Dict[str, Any]) -> Tuple[str, str]:
        pkg = str(d.get("package") or "").strip()
        bonuses = d.get("bonuses", d.get("bonus", d.get("extra_bonuses", d.get("extra_services"))))
        return pkg, str(bonuses or "").strip()

    for i, d in enumerate(deals_chunk):
        title = _title(d)
        date_s, time_s = _deal_dt_parts(d)
        pkg_s, bonus_s = _deal_extras(d)
        parts = [f"🎉 {title} — {date_s} {time_s}"]
        if pkg_s:
            parts.append(pkg_s)
        if bonus_s:
            parts.append(bonus_s)
        opts.append(truncate(" ".join(parts)))
        idx_map[i] = int(d.get("id", 0))

    opts += ["🚫 Не смогу работать", "🛡️ Могу Администратором"]

    poll = await bot.send_poll(
        chat_id,
        "🚨 Срочные игры",
        opts,
        is_anonymous=False,
        allows_multiple_answers=True,
    )
    state.responses[poll.poll.id] = {
        "deals": {int(d.get("id", 0)): [] for d in deals_chunk},
        "not_available": [],
        "admin_available": [],
        "deal_indices": idx_map,
    }

    # Добавим эти сделки в текущий цикл и очистим буфер «новых»
    existing_ids = {int(d.get("id", 0)) for d in (state.current_poll_deals or [])}
    state.current_poll_deals.extend([d for d in deals_chunk if int(d.get("id", 0)) not in existing_ids])
    state.pending_new_deals = []

    # Перерисуем отчёт
    await _sync_leader_report()

    with contextlib.suppress(Exception):
        await callback.answer("Опрос по срочным играм отправлен.", show_alert=False)

# История изменений (2025-08-29):
# • Реализована кнопка «Новая игра» → отдельный опрос «Срочные игры» + включение игр в текущий цикл.



# ════════════════════════════════════════════════════════════════════
# [4] ОТЧЁТ ЛИДЕРУ / ПРИЁМ ОТВЕТОВ — редактирование «на месте»
# Версия 6.9 · 2025-08-30 (скрытие завершённых игр: фикс суффиксов + свежие снапшоты)
# ────────────────────────────────────────────────────────────────────
# • Используем _edit_or_send_report() из [0.96] → отчёт не копится в ЛС.
# • NEW: _refresh_deal_snapshots_for_report() — обновляем теги/статус для залоченных сделок
#         (только нужные id, с кэшем по времени), чтобы фильтр скрытия видел реальные данные.
# • FIX: _compose_tag больше не дублирует суффиксы (.1/.2/.Адм), если они уже в имени.
# ════════════════════════════════════════════════════════════════════

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta
import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ХЕЛПЕРЫ ДЛЯ «НОВЫХ ИГР» (SSOT, безопасные фолбэки)
# ────────────────────────────────────────────────────────────────────

def _event_dt(d: Dict[str, Any]) -> Optional[datetime]:
    dt = d.get("event_datetime")
    return dt if isinstance(dt, datetime) else None

def _is_within_window(dt: Optional[datetime]) -> bool:
    try:
        days = int(getattr(settings, "POLL_WINDOW_DAYS", 0) or 10)
    except Exception:
        days = 10
    days = max(1, days)
    if not isinstance(dt, datetime):
        return False
    now = datetime.now(tz=MSK_TZ)
    return now <= dt <= (now + timedelta(days=days))

def _is_status(d: Dict[str, Any], status_id: Optional[int]) -> bool:
    try:
        return int(d.get("status_id")) == int(status_id) if status_id is not None else False
    except Exception:
        return False

def _is_preliminary_status(d: Dict[str, Any]) -> bool:
    try:
        pre_id = getattr(settings, "PRE_APPLICATION_STATUS_ID", None)
        if pre_id is not None and d.get("status_id") == pre_id:
            return True
    except Exception:
        pass
    name = str(d.get("status_name") or d.get("pipeline_status_name") or "").lower()
    return "предвар" in name

def _is_bron_status(d: Dict[str, Any]) -> bool:
    return _is_status(d, getattr(settings, "BRON_STATUS_ID", None))

def _is_success_status(d: Dict[str, Any]) -> bool:
    """Понимаем «Завершение сделки»/успех: по SUCCESSFUL_STATUS_ID или эвристике имени."""
    try:
        succ_id = getattr(settings, "SUCCESSFUL_STATUS_ID", None)
        if succ_id is not None and d.get("status_id") == succ_id:
            return True
    except Exception:
        pass
    name = str(d.get("status_name") or d.get("pipeline_status_name") or "").lower()
    return any(k in name for k in ("заверш", "успеш", "реализ", "закрыт"))

_ROLE_TAG_RE = re.compile(r".+\.(?:1|2|Адм|Стаж)$", re.IGNORECASE | re.UNICODE)

def _has_leader_tags(d: Dict[str, Any]) -> bool:
    """Есть ли в сделке ролевые теги ('.1', '.2', '.Адм', '.Стаж')."""
    tags = d.get("tags") or []
    names: List[str] = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                names.append(t)
            elif isinstance(t, dict) and "name" in t:
                names.append(str(t["name"]))
    for name in names:
        if _ROLE_TAG_RE.match(name.strip()):
            return True
    return False

def _deal_id_set(arr: List[Dict[str, Any]]) -> Set[int]:
    out: Set[int] = set()
    for d in arr or []:
        try:
            did = int(d.get("id", 0))
            if did:
                out.add(did)
        except Exception:
            continue
    return out

# ────────────────────────────────────────────────────────────────────
# СВЕЖИЕ СНЭПШОТЫ ДЛЯ ЗАКРЫТЫХ СДЕЛОК (теги/статус)
# ────────────────────────────────────────────────────────────────────

async def _refresh_deal_snapshots_for_report() -> None:
    """
    Обновляет в state.current_poll_deals поля tags/status_* для СДЕЛОК,
    которые уже в locked_distribution (т.е. потенциально должны исчезнуть).
    С кэшем по времени: не чаще, чем раз в 20 секунд на сделку.
    Все сетевые вызовы — через services.amocrm.get_deal_by_id (SSOT).
    """
    deals = getattr(state, "current_poll_deals", []) or []
    if not deals:
        return

    locked_map = getattr(state, "locked_distribution", {}) or {}
    if not locked_map:
        return

    want_ids: List[int] = []
    index: Dict[int, int] = {}
    for i, d in enumerate(deals):
        try:
            did = int(d.get("id", 0))
        except Exception:
            continue
        if did and did in locked_map:
            want_ids.append(did)
            index[did] = i

    if not want_ids:
        return

    ts = getattr(state, "deal_snapshots_ts", {}) or {}
    now = datetime.now(tz=MSK_TZ).timestamp()

    # ленивый импорт сервиса
    try:
        from services.amocrm import get_deal_by_id  # type: ignore
    except Exception:
        async def get_deal_by_id(_did: int) -> Optional[Dict[str, Any]]:  # type: ignore
            return None

    for did in want_ids:
        try:
            last = float(ts.get(did, 0.0))
        except Exception:
            last = 0.0
        if now - last < 20.0:
            continue  # кэш ещё свежий

        try:
            snap = await get_deal_by_id(int(did))
        except Exception as e:
            logger.debug("[report] get_deal_by_id(%s) failed: %s", did, e)
            continue
        if not isinstance(snap, dict):
            continue

        # аккуратно мержим только то, что важно для скрытия
        i = index.get(did)
        if i is None:
            continue
        base = dict(deals[i])
        for k in ("tags", "status_id", "status_name", "pipeline_status_name"):
            if k in snap:
                base[k] = snap[k]
        deals[i] = base  # обновили снапшот в списке
        ts[did] = now

    setattr(state, "current_poll_deals", deals)
    setattr(state, "deal_snapshots_ts", ts)

# ────────────────────────────────────────────────────────────────────
# COMPAT: shim для старых/рассинхроненных сборок — чтобы Пайланс не ругался.
# Если реальная _refresh_pending_new_games уже определена в другом блоке,
# этот shim НЕ переопределит её.
# ────────────────────────────────────────────────────────────────────
if "_refresh_pending_new_games" not in globals():
    async def _refresh_pending_new_games() -> int:
        """No-op: обновление буфера «новых игр». Возвращает 0, если не реализовано."""
        return 0

# ────────────────────────────────────────────────────────────────────
# ПОСТРОИТЕЛЬ ОТЧЁТА/КЛАВИАТУРЫ + ФИЛЬТРАЦИЯ ЗАВЕРШЁННЫХ ИГР
# ────────────────────────────────────────────────────────────────────

def _compose_tag(base_name: str, suffix: str) -> str:
    """
    Собирает ожидаемый тег «Имя Ф.» + суффикс (1/2/Адм), без дублирования.
    Если base уже содержит .1/.2/.Адм/.Стаж — возвращаем base как есть.
    """
    base = (base_name or "").strip()
    if not base:
        return ""
    if re.search(r"\.(?:1|2|Адм|Стаж)$", base, flags=re.IGNORECASE):
        return base.lower()
    suf = suffix.lstrip(".")
    return f"{base}.{suf}".lower()

def _expected_tags_for_locked(d: Dict[str, Any], locked_map: Dict[int, Any]) -> Set[str]:
    """
    Возвращает набор ожидаемых тегов для УТВЕРЖДЁННОГО состава сделки:
    • lead*  → 'Имя Ф.1'
    • assistant* → 'Имя Ф.2'
    • admin → 'Имя Ф.Адм'
    • trainee игнорируем
    """
    did = int(d.get("id") or 0)
    expected: Set[str] = set()
    dist = locked_map.get(did) or {}
    if not isinstance(dist, dict):
        return expected

    def _vals(x: Any) -> List[str]:
        if isinstance(x, (list, tuple, set)):
            return [str(v) for v in x]
        return [str(x)] if x not in (None, "", 0) else []

    for key, val in dist.items():
        if not isinstance(key, str):
            continue
        slot = key.lower().strip()
        labels = _vals(val)
        if not labels:
            continue
        if slot.startswith("lead"):
            for lab in labels:
                base = lab.split("|", 1)[0].strip()
                if base:
                    expected.add(_compose_tag(base, "1"))
        elif slot.startswith("assistant"):
            for lab in labels:
                base = lab.split("|", 1)[0].strip()
                if base:
                    expected.add(_compose_tag(base, "2"))
        elif slot == "admin":
            for lab in labels:
                base = lab.split("|", 1)[0].strip()
                if base:
                    expected.add(_compose_tag(base, "Адм"))
        # trainee — игнор
    return {e for e in expected if e}

def _deal_tags_lower_set(d: Dict[str, Any]) -> Set[str]:
    tags = d.get("tags") or []
    names: List[str] = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                names.append(t)
            elif isinstance(t, dict) and "name" in t:
                names.append(str(t["name"]))
    return {n.strip().lower() for n in names if n and isinstance(n, str)}

def _should_hide_in_report_sync(d: Dict[str, Any], locked_map: Dict[int, Any]) -> bool:
    """
    True — игру скрываем из «Отчёта по опросу».

    Правила:
    • Сделка в locked_distribution (утверждена лидером).
    • В CRM присутствуют ВСЕ ожидаемые теги утверждённого состава (.1/.2/.Адм).
      Стажёры для скрытия не требуются.
    • Если статус «Бронь» → дополнительно требуется «Завершение сделки».
    • Для прочих статусов — скрываем сразу после постановки всех тегов.
    """
    did = int(d.get("id") or 0)
    if not did or did not in locked_map:
        return False

    expected = _expected_tags_for_locked(d, locked_map)
    if not expected:
        return False

    have = _deal_tags_lower_set(d)
    if not expected.issubset(have):
        return False

    if _is_bron_status(d):
        return _is_success_status(d)
    return True

async def generate_poll_report() -> str:
    """
    Короткий заголовок отчёта; галочки/кнопки — в клавиатуре.
    Побочно: освежаем буфер «Новые игры» и ОБНОВЛЯЕМ снапшоты для залоченных сделок.
    """
    await _refresh_pending_new_games()
    await _refresh_deal_snapshots_for_report()
    return "📊 Выберите игру, чтобы открыть детали."

def _counts_ready_for_deal(deal: Dict[str, Any]) -> Tuple[bool, Dict[str, int]]:
    """
    Считает готовность по state.distribution_cache[str(deal_id)] c защитой от дублей.
    Правило «1 пользователь = 1 роль».
    """
    did = int(deal.get("id") or 0)
    g_name = str(deal.get("game_name") or deal.get("name") or "")
    pkg = str(deal.get("package") or "")
    cfg = _role_cfg(g_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))
    need_admin = _need_admin_by_package(pkg)

    dist: Dict[str, Any] = (getattr(state, "distribution_cache", {}) or {}).get(str(did), {}) or {}

    main_uids = [_slot_uid(dist.get(f"lead{i}")) for i in range(1, max(1, need_main) + 1)]
    assist_uids = [_slot_uid(dist.get(f"assistant{i}")) for i in range(1, max(0, need_assist) + 1)]
    admin_uid = _slot_uid(dist.get("admin"))

    ms = [u for u in main_uids if u]
    as_ = [u for u in assist_uids if u]
    as_uniq = [u for u in as_ if u not in ms]
    have_main = len(set(ms))
    have_assist = len(set(as_uniq))
    # FIX (пайланс/логика): считаем have_admin только если слот реально занят уникальным uid
    have_admin = 1 if (admin_uid and admin_uid not in ms and admin_uid not in as_) else 0

    admin_ok = (need_admin == 0) or (have_admin >= 1)
    ready = (have_main >= need_main) and (have_assist >= need_assist) and admin_ok

    return ready, {
        "have_main": have_main, "need_main": need_main,
        "have_assist": have_assist, "need_assist": need_assist,
        "have_admin": have_admin, "need_admin": need_admin,
    }

def _build_report_keyboard() -> InlineKeyboardMarkup:
    """
    Список игр: статус ✅/❌ + название + дата/время.
    Справа: «👍 Утвердить» для готовых, «✅ Утверждено» для зафиксированных.
    Вверху: «🆕 Новая игра», если есть pending_new_deals.
    Внизу: «Утвердить все», если есть готовые незалоченные.
    Скрываем игры, если _should_hide_in_report_sync(...) == True.
    """
    rows: List[List[InlineKeyboardButton]] = []
    any_ready_unlocked = False

    # Набор залоченных сделок
    raw_locked = (getattr(state, "locked_distribution", {}) or {})
    locked_map: Dict[int, Any] = {}
    for k, v in raw_locked.items():
        try:
            locked_map[int(k)] = v
        except Exception:
            continue

    # Кнопка «Новая игра»
    try:
        pending_cnt = len(getattr(state, "pending_new_deals", []) or [])
        if getattr(state, "coordination_cycle_active", False) and pending_cnt > 0:
            rows.append([InlineKeyboardButton(text=f"🆕 Новая игра ({pending_cnt})", callback_data="poll_new_game")])
    except Exception:
        pass

    # Фильтрация завершённых
    filtered_deals: List[Dict[str, Any]] = []
    for d in (state.current_poll_deals or []):
        did = int(d.get("id") or 0)
        if not did or did in (state.deal_force_closed or set()):
            continue
        try:
            if _should_hide_in_report_sync(d, locked_map):
                continue
        except Exception as e:
            logger.debug("[report] hide-check failed deal=%s: %s", did, e)
        filtered_deals.append(d)

    for d in filtered_deals:
        did = int(d.get("id") or 0)
        ready, _ = _counts_ready_for_deal(d)

        base_name = str(d.get("game_name") or d.get("name") or "Игра")
        name = f"{base_name} (предварительно)" if _is_preliminary_status(d) else base_name

        dt = d.get("event_datetime")
        date_s = dt.strftime("%d.%m") if hasattr(dt, "strftime") else str(d.get("event_date") or "—")
        time_s = str(d.get("event_time") or "—")

        left = InlineKeyboardButton(
            text=f"{'✅' if (ready or did in locked_map) else '❌'} {name} · {date_s} {time_s}",
            callback_data=f"show_deal_{did}"
        )
        row = [left]

        if did in locked_map:
            row.append(InlineKeyboardButton(text="✅ Утверждено", callback_data="noop"))
        elif ready:
            row.append(InlineKeyboardButton(text="👍 Утвердить", callback_data=f"poll_approve_{did}"))
            any_ready_unlocked = True

        rows.append(row)

    if any_ready_unlocked:
        rows.append([InlineKeyboardButton(text="Утвердить все", callback_data="approve_all_ready")])

    # actions из polls_distribution (если есть)
    try:
        from handlers.polls_distribution import distribution_actions_markup  # lazy import
        actions = distribution_actions_markup()
        rows.extend(actions.inline_keyboard or [])
    except Exception:
        pass

    return InlineKeyboardMarkup(
        inline_keyboard=rows if rows else [[InlineKeyboardButton(text="Обновить", callback_data="poll_back_to_games_list")]]
    )

# ────────────────────────────────────────────────────────────────────
# ПРИЁМ ОТВЕТОВ ПОЛЛА (без изменений логики распределения)
# ────────────────────────────────────────────────────────────────────

@router.poll_answer()
async def handle_poll_answer(event: types.PollAnswer) -> None:
    """
    Фиксируем текущее состояние ответов пользователя:
    • удаляем его прошлые следы из текущего poll-чунка (deals/not_available/admin_available);
    • добавляем новые выборы;
    • пересчитываем distribution_cache как минимум для:
        – всех сделок, из которых пользователь был удалён (отмена голоса);
        – всех сделок, на которые он только что откликнулся;
      и для ВСЕХ игр цикла, если изменился флаг «🛡️ Админом».
    """
    uid: int = event.user.id
    poll_id: str = event.poll_id
    chosen: List[int] = list(event.option_ids or [])

    data = (state.responses or {}).get(poll_id)
    if not isinstance(data, dict):
        return

    logger.debug("[answer] uid=%d poll=%s choices=%s", uid, poll_id, chosen)

    # --- 0) собрать «прежние» следы пользователя в этом poll-чунке
    prev_impacted: Set[int] = set()
    try:
        deals_map: Dict[int, List[Dict[str, Any]]] = data.get("deals") or {}
        for did, arr in deals_map.items():
            if any(int(u.get("user_id", 0)) == uid for u in (arr or [])):
                prev_impacted.add(int(did))
    except Exception:
        pass

    prev_admin_flag = False
    try:
        prev_admin_flag = any(int(u.get("user_id", 0)) == uid for u in (data.get("admin_available") or []))
    except Exception:
        prev_admin_flag = False

    # --- 1) удалить старые следы пользователя из этого poll-чунка
    for lst in (data.get("deals") or {}).values():
        lst[:] = [u for u in (lst or []) if int(u.get("user_id", 0)) != uid]
    data["not_available"][:] = [u for u in (data.get("not_available") or []) if int(u.get("user_id", 0)) != uid]
    data["admin_available"][:] = [u for u in (data.get("admin_available") or []) if int(u.get("user_id", 0)) != uid]

    # --- 2) записать новые выборы пользователя
    ui = await get_user_info(uid) or {}
    base = {
        "user_id": uid,
        "first_name": ui.get("first_name", ""),
        "last_name_initial": ui.get("last_name_initial", ""),
        "is_admin_eligible": False,
    }

    deal_indices: Dict[int, int] = data.get("deal_indices") or {}
    deals_count = len(deal_indices)
    new_impacted: Set[int] = set()
    new_admin_flag = False

    for idx in chosen:
        if idx < deals_count:
            did = int(deal_indices[idx])
            if did not in (state.deal_force_closed or set()):
                (data["deals"][did]).append(dict(base))
                new_impacted.add(did)
        elif idx == deals_count:
            (data["not_available"]).append(dict(base))
        else:
            adm = dict(base); adm["is_admin_eligible"] = True
            (data["admin_available"]).append(adm)
            new_admin_flag = True

    # --- 3) определить область пересчёта
    impacted: Set[int] = set(prev_impacted) | set(new_impacted)
    admin_flag_changed: bool = (new_admin_flag != prev_admin_flag)

    if admin_flag_changed:
        impacted = {int(d.get("id", 0)) for d in (state.current_poll_deals or []) if int(d.get("id", 0))}
    if not impacted and prev_impacted:
        impacted = set(prev_impacted)

    # --- 4) автораспределение (обновит distribution_cache)
    await _auto_assign_from_responses(
        impacted=impacted,
        apply_to_all_on_admin_flag=admin_flag_changed or new_admin_flag,
    )

    # --- 5) эффекты UI/индикации
    await _sync_leader_report()  # отчёт лидеру всегда «редактируется на месте»
    await _check_ready_state(impacted or set())
    asyncio.create_task(_refresh_detail_views(impacted or set(), refresh_all=admin_flag_changed))

    logger.debug(
        "[answer] uid=%d prev_impacted=%s new_impacted=%s admin_changed=%s → recomputed=%s",
        uid, sorted(prev_impacted), sorted(new_impacted), admin_flag_changed, sorted(impacted),
    )

# История изменений:
# 2025-08-30 — v6.9: _compose_tag не дублирует суффиксы; добавлен _refresh_deal_snapshots_for_report();
#                    generate_poll_report освежает снапшоты → игры с полными тегами (+ SUCCESS для «Бронь») скрываются.



# ════════════════════════════════════════════════════════════════════
# [4.8] HANDLER: «Замена» из «Мои игры» (mygame_swap_{deal_id})
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith("mygame_swap_"))
async def swap_request_handler(callback: types.CallbackQuery) -> None:
    """
    Пользователь просит замену на уже утверждённую игру.
    Делаем:
      • проверяем, что пользователь действительно назначен (по locked_distribution/cache);
      • публикуем объявление в рабочем чате с кнопкой «✋ Откликнуться»;
      • мягко чистим его подтверждающие теги в сделке и переводим статус в «Бронь» (pre-flight внутри);
      • не ломаем текущий состав локально — перераспределение произойдёт после «Откликнуться».
    """
    uid = int(callback.from_user.id)
    data_s = str(callback.data or "")
    try:
        deal_id = int(data_s.split("_")[-1])
    except Exception:
        await callback.answer("⚠️ Ошибочная кнопка.", show_alert=True)
        return

    logger.info("[swap] request by uid=%s deal_id=%s", uid, deal_id)

    # проверим, что пользователь назначен на эту игру и какую роль он занимает
    role = _assigned_role_from_state(uid, deal_id)
    if not role:
        await callback.answer("Вы не назначены на эту игру.", show_alert=True)
        logger.warning("[swap] denied: uid=%s not assigned to deal=%s", uid, deal_id)
        return

    # найдём «снимок» сделки для текста
    snap = await _find_deal_snapshot(deal_id)
    title = _deal_title(snap or {"id": deal_id})
    dt = (snap or {}).get("event_datetime")
    date_s = dt.strftime("%d.%m") if hasattr(dt, "strftime") else str((snap or {}).get("event_date") or "—")
    time_s = str((snap or {}).get("event_time") or "—")
    pkg = str((snap or {}).get("package") or "—")
    players = str((snap or {}).get("players") or "—")

    # куда публикуем — общий резолвер из SSOT
    bot = Bot.get_current()
    chat_id = resolve_notify_chat_id(bot)
    if not chat_id:
        await callback.answer("⚠️ Не настроен чат для объявлений.", show_alert=True)
        logger.error("[swap] no available chat for notify; uid=%s deal=%s", uid, deal_id)
        return

    # объявление об открытой замене + кнопка отклика
    short = await short_name(uid)
    text = (
        f"⚠️ {short} просит замену на игру «{title}»\n"
        f"📅 {date_s} · 🕒 {time_s} · 📦 {pkg} · 👥 {players}\n\n"
        "Нажмите «Откликнуться», если готовы выйти."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✋ Откликнуться", callback_data=f"swap_accept_{deal_id}_{role}")]
        ]
    )

    # запомним открытый запрос замены (первый клик «Откликнуться» выигрывает)
    if not getattr(state, "swap_requests", None):
        state.swap_requests = {}
    state.swap_requests[int(deal_id)] = {
        "by": uid,
        "role": role,
        "accepted_by": None,
        "created_at": datetime.now(tz=MSK_TZ).isoformat(),
    }

    # Пытаемся отправить в рабочий чат. Если получится — отлично.
    sent_ok = False
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        sent_ok = True
        logger.info("[swap] announcement posted to chat=%s for deal=%s", chat_id, deal_id)
    except Exception as e:
        logger.warning("[swap] notify failed chat=%s deal=%s: %s", chat_id, deal_id, e)

    # Фолбэк: если не смогли отправить в чат — уведомим инициатора в ЛС,
    # чтобы они понимали, что запрос зафиксирован, и дадим текст для ручного форварда.
    if not sent_ok:
        try:
            await bot.send_message(
                uid,
                "⚠️ Не удалось отправить объявление в чат. "
                "Я сохранил запрос на замену. "
                "Скинь этот текст в рабочий чат вручную:\n\n" + text
            )
        except Exception:
            pass

    # CRM: убрать подтверждающие теги пользователя и вернуть сделку в «Бронь»
    try:
        from services.amocrm import revert_to_bron_after_swap  # lazy import
        await revert_to_bron_after_swap(int(deal_id), uid=uid, short_base=await short_name(uid))
    except Exception as e:
        logger.warning("[swap] CRM revert failed for deal=%s: %s", deal_id, e)

    # UI: сообщим пользователю и мягко обновим отчёты/детали
    with contextlib.suppress(Exception):
        await callback.answer("Запрос на замену отправлен.", show_alert=False)
    await _sync_leader_report()
    asyncio.create_task(_refresh_detail_views({int(deal_id)}, refresh_all=False))


# ════════════════════════════════════════════════════════════════════
# [4.9] HANDLER: «Откликнуться» на замену (swap_accept_{deal_id}_{role})
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith("swap_accept_"))
async def swap_accept_handler(callback: types.CallbackQuery) -> None:
    """
    Первый клик выигрывает:
    • фиксируем «accepted_by» в state.swap_requests[deal_id], остальные получают ACK «уже занято»;
    • проверяем по «Светофору» (green/yellow ок для core-ролей; red → стажёр);
    • точечно пересобираем distribution_cache по сделке (без трогания locked_distribution);
    • уведомляем чат: «Состав команды обновлён … Подтвердите участие в личном кабинете»;
    • триггерим _sync_leader_report, _check_ready_state и перерисовку деталей.
    """
    data = str(callback.data or "")
    try:
        # swap_accept_{deal_id}_{role}
        _, _, tail = data.partition("swap_accept_")
        lead_s, role_raw = tail.rsplit("_", 1)
        deal_id = int(lead_s)
        role = role_raw.strip().lower()
    except Exception:
        await callback.answer("⚠️ Ошибочная кнопка.", show_alert=True)
        return

    uid = int(callback.from_user.id)
    logger.info("[swap] accept candidate uid=%s deal=%s role=%s", uid, deal_id, role)

    req = (getattr(state, "swap_requests", {}) or {}).get(deal_id)
    if not isinstance(req, dict):
        await callback.answer("Запрос замены уже закрыт.", show_alert=True)
        logger.warning("[swap] accept: no open request for deal=%s", deal_id)
        return

    # запрет самопринятия (инициатор не может принять сам себя)
    if int(req.get("by") or 0) == uid:
        await callback.answer("Вы уже запрашивали замену на эту игру.", show_alert=True)
        logger.info("[swap] accept: same user tried to accept own swap uid=%s deal=%s", uid, deal_id)
        return

    # «первый клик выигрывает»
    accepted = req.get("accepted_by")
    if accepted is not None:
        await callback.answer("Уже занято — замена назначена.", show_alert=True)
        logger.info("[swap] accept: already taken by uid=%s", accepted)
        return

    # фиксируем победителя без промежуточных await, чтобы минимизировать гонки
    req["accepted_by"] = uid

    # проверка по «Светофору»
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == int(deal_id)), {}) or {}
    if not deal:
        # фолбэк: достанем снапшот из AmoCRM
        deal = await _find_deal_snapshot(deal_id)
    game_name = str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal_id}")
    status = await _sv_status(uid, game_name)  # '' | green | yellow | red

    # точечная пересборка распределения под кандидата
    dist = await _insert_candidate_into_distribution(deal_id=deal_id, role=role, uid=uid, status=status)

    # уведомление в чат
    bot = Bot.get_current()
    chat_id = resolve_notify_chat_id(bot)
    if chat_id:
        try:
            title = _deal_title(deal)
            dt = deal.get("event_datetime")
            date_s = dt.strftime("%d.%m") if hasattr(dt, "strftime") else str(deal.get("event_date") or "—")
            time_s = str(deal.get("event_time") or "—")

            # Печать состава — строго через SSOT-хелпер
            summary_lines = await team_bulleted_lines(dist)
            summary = "\n".join(summary_lines)

            text = (
                "✅ Состав команды обновлён.\n"
                f"🎮 «{title}» — {date_s} {time_s}\n"
                f"{summary}\n\n"
                "Подтвердите участие в личном кабинете."
            )
            await bot.send_message(chat_id, text)
            logger.info("[swap] updated team posted to chat=%s for deal=%s", chat_id, deal_id)
        except Exception as e:
            logger.warning("[swap] chat notify failed for deal=%s: %s", deal_id, e)

    # локальные эффекты UI/индикаторов
    impacted = {int(deal_id)}
    await _sync_leader_report()
    await _check_ready_state(impacted)
    asyncio.create_task(_refresh_detail_views(impacted, refresh_all=False))

    with contextlib.suppress(Exception):
        await callback.answer("Спасибо! Вы в составе на эту игру.", show_alert=False)


# ────────────────────────────────────────────────────────────────────
# ПОСТРОИТЕЛЬ КЛАВИАТУРЫ ОТЧЁТА + ЕДИНЫЙ ФИЛЬТР «АКТИВНОСТИ ДЛЯ РАСПРЕДЕЛЕНИЯ»
# ────────────────────────────────────────────────────────────────────

def _is_active_for_distribution(d: Dict[str, Any], locked_map: Dict[int, Any]) -> bool:
    """
    ЕДИНЫЙ фильтр «активности для распределения» в отчёте.

    Игра считается АКТИВНОЙ (показываем в списке), если:
      • у неё валидный id;
      • она НЕ помечена как снятая вручную (state.deal_force_closed);
      • она НЕ имеет успешного статуса (SUCCESSFUL_STATUS_ID) по локальному снапшоту/state;
      • она НЕ подпадает под правила скрытия _should_hide_in_report_sync(...)
        (т.е. ещё не полностью закрыта по тегам/статусу).

    Важное требование задачи:
      • При скрытии «снятых»/«успешных» пишем лог:
        [report] hide finished/closed deal in report: {deal_id}
    """
    try:
        did = int(d.get("id") or 0)
    except Exception:
        return False
    if not did:
        return False

    # 1) Снятые/закрытые вручную (deal_force_closed)
    closed_set: Set[int] = getattr(state, "deal_force_closed", set()) or set()
    if did in closed_set:
        logger.info("[report] hide finished/closed deal in report: %s", did)
        return False

    # 2) Успешные по локальному снапшоту или текущему dict сделки
    def _status_from_snapshot(did_: int, deal: Dict[str, Any]) -> Optional[int]:
        snap = getattr(state, "current_poll_snapshot", None)
        # dict: {id|str(id): {...}|status_id}
        if isinstance(snap, dict):
            val = snap.get(did_, snap.get(str(did_)))
            if isinstance(val, dict):
                sid = val.get("status_id", val.get("statusId"))
            else:
                sid = val
            try:
                return int(sid) if sid is not None else None
            except Exception:
                return None
        # list[dict]
        if isinstance(snap, list):
            for item in snap:
                if isinstance(item, dict):
                    try:
                        if int(item.get("id", 0) or 0) == did_:
                            sid = item.get("status_id", item.get("statusId"))
                            return int(sid) if sid is not None else None
                    except Exception:
                        continue
        # фолбэк: прямо из сделки
        try:
            sid = deal.get("status_id", deal.get("statusId"))
            return int(sid) if sid is not None else None
        except Exception:
            return None

    succ_id = getattr(settings, "SUCCESSFUL_STATUS_ID", None)
    if succ_id is not None:
        sid = _status_from_snapshot(did, d)
        if sid is not None and int(sid) == int(succ_id):
            logger.info("[report] hide finished/closed deal in report: %s", did)
            return False

    # 3) Скрытие «полностью закрытых» по тегам/статусу (существующая логика)
    try:
        return not _should_hide_in_report_sync(d, locked_map)
    except Exception:
        # На любой ошибке — показываем, чтобы не потерять игру из отчёта
        return True


def _build_report_keyboard() -> InlineKeyboardMarkup:
    """
    Список игр: статус ✅/❌ + название + дата/время.
    Справа: «👍 Утвердить» для готовых, «✅ Утверждено» для зафиксированных.
    Вверху: «🆕 Новая игра», если есть pending_new_deals.
    Внизу: «Утвердить все», если есть готовые незалоченные.
    Отбор игр — через единый фильтр _is_active_for_distribution(...).
    """
    rows: List[List[InlineKeyboardButton]] = []
    any_ready_unlocked = False

    # Набор залоченных сделок (int-ключи)
    raw_locked = (getattr(state, "locked_distribution", {}) or {})
    locked_map: Dict[int, Any] = {}
    for k, v in raw_locked.items():
        try:
            locked_map[int(k)] = v
        except Exception:
            continue

    # Кнопка «Новая игра»
    try:
        pending_cnt = len(getattr(state, "pending_new_deals", []) or [])
        if getattr(state, "coordination_cycle_active", False) and pending_cnt > 0:
            rows.append([InlineKeyboardButton(text=f"🆕 Новая игра ({pending_cnt})", callback_data="poll_new_game")])
    except Exception:
        pass

    # Нормализация времени — только из кастомного поля event_time (фолбэк на event_datetime != 00:00)
    def _norm_time(deal: Dict[str, Any]) -> str:
        try:
            _norm = globals().get("_normalize_time_str")
            ts = _norm(str(deal.get("event_time") or "")) if callable(_norm) else str(deal.get("event_time") or "")
        except Exception:
            ts = str(deal.get("event_time") or "")
        ts = ts.replace(".", ":").strip()
        if ts:
            return ts
        dt = deal.get("event_datetime")
        if hasattr(dt, "strftime"):
            hhmm = dt.strftime("%H:%M")
            if hhmm != "00:00":  # не сбрасывать в 00:00
                return hhmm
        return "—"

    # Фильтрация перед рендером
    filtered_deals: List[Dict[str, Any]] = []
    for d in (state.current_poll_deals or []):
        try:
            if _is_active_for_distribution(d, locked_map):
                filtered_deals.append(d)
        except Exception as e:
            logger.debug("[report] active-filter fail deal=%s: %s", d.get("id"), e)

    # Построение строк клавиатуры
    for d in filtered_deals:
        try:
            did = int(d.get("id") or 0)
        except Exception:
            continue
        if not did:
            continue

        ready, _ = _counts_ready_for_deal(d)

        base_name = str(d.get("game_name") or d.get("name") or "Игра")
        name = f"{base_name} (предварительно)" if _is_preliminary_status(d) else base_name

        dt = d.get("event_datetime")
        date_s = dt.strftime("%d.%m") if hasattr(dt, "strftime") else str(d.get("event_date") or "—")
        time_s = _norm_time(d)

        left = InlineKeyboardButton(
            text=f"{'✅' if (ready or did in locked_map) else '❌'} {name} · {date_s} {time_s}",
            callback_data=f"show_deal_{did}",
        )
        row: List[InlineKeyboardButton] = [left]

        if did in locked_map:
            row.append(InlineKeyboardButton(text="✅ Утверждено", callback_data="noop"))
        elif ready:
            row.append(InlineKeyboardButton(text="👍 Утвердить", callback_data=f"poll_approve_{did}"))
            any_ready_unlocked = True

        rows.append(row)

    if any_ready_unlocked:
        rows.append([InlineKeyboardButton(text="Утвердить все", callback_data="approve_all_ready")])

    # actions из polls_distribution (если есть)
    try:
        from handlers.polls_distribution import distribution_actions_markup  # lazy import
        actions = distribution_actions_markup()
        rows.extend(actions.inline_keyboard or [])
    except Exception:
        pass

    return InlineKeyboardMarkup(
        inline_keyboard=rows if rows else [[InlineKeyboardButton(text="Обновить", callback_data="poll_back_to_games_list")]]
    )

# История изменений:
# 2025-08-31 · отчёт скрывает закрытые/успешные/снятые сделки (deal_force_closed)


# ════════════════════════════════════════════════════════════════════
# [5] ГОТОВНОСТЬ И УВЕДОМЛЕНИЯ
# ════════════════════════════════════════════════════════════════════
async def _is_deal_ready(did: int) -> bool:
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0) or 0) == int(did)), None)
    if not deal:
        return False
    ready, _ = _counts_ready_for_deal(deal)
    return ready

async def _check_ready_state(impacted: Set[int]) -> None:
    """
    Пересчитывает готовность игр и отправляет краткое уведомление в чат цикла.
    FIX: исправлен формат даты — русская «м» заменена на латинскую: %d.%m.
    """
    bot = Bot.get_current()
    chat_id = state.admin_chat_id or state.current_poll_leader
    if not chat_id:
        return

    newly: List[str] = []

    for did in (impacted or set()):
        if await _is_deal_ready(did) and not (state.current_deal_ready or {}).get(did):
            state.current_deal_ready[did] = True
            deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0) or 0) == int(did)), None)
            if not deal:
                continue

            dt = deal.get("event_datetime")
            try:
                # ✔ Правильный формат: %d.%m (латинская m)
                date_s = dt.strftime("%d.%m") if hasattr(dt, "strftime") else str(deal.get("event_date") or "—")
            except Exception:
                date_s = str(deal.get("event_date") or "—")

            title = _deal_title(deal)
            newly.append(f"{title} — {date_s}")

    if newly:
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id,
                "✅ *Предварительный состав команды на игру набран!*\n"
                "Успейте отметиться в опросе, чтобы участвовать в распределении:\n"
                + "\n".join(f"• {n}" for n in newly),
                parse_mode="Markdown",
            )

    # Если все игры цикла «готовы» — единоразовое уведомление
    if (
        state.current_poll_deals
        and all((state.current_deal_ready or {}).get(int(d.get("id", 0) or 0)) for d in state.current_poll_deals)
        and not state.all_ready_notified
    ):
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id,
                "✅ Для всех игр определён предварительный состав команды. "
                "Успейте отметиться в опросе, чтобы участвовать в распределении.",
                parse_mode="Markdown",
            )
        state.all_ready_notified = True

# История изменений: 2025-08-14 — фикс формата даты в _check_ready_state (%d.%m)



# ════════════════════════════════════════════════════════════════════
# [6] ЗАВЕРШЕНИЕ ЦИКЛА / ВСПОМОГАТЕЛЬНЫЕ ЭКСПОРТЫ
# ════════════════════════════════════════════════════════════════════
async def clear_poll_data(uid: int) -> None:
    """Завершает текущий цикл опроса и подчищает состояние + ЛС."""
    if not state.coordination_cycle_active:
        return
    state.coordination_cycle_active = False
    state.force_closed = True
    _cancel_reminders()

    # очистка временных структур
    state.current_deal_ready.clear()
    state.responses.clear()
    state.poll_distribution.clear()
    state.current_poll_deals.clear()
    state.deal_force_closed.clear()

    # пылесос у лидера
    if uid:
        with contextlib.suppress(Exception):
            await _vacuum_old_messages()


async def hide_deal_from_report(deal_id: int) -> None:
    """
    Помечает сделку завершённой для отчёта по опросу.
    Реализация: добавить deal_id в state.deal_force_closed (set[int]).
    Поддержать ключ и как int, и как str, т.к. в state встречаются оба варианта.
    Логи: [lifecycle] hide deal from report: {deal_id}
    """
    # Если набора нет — создаём
    if not isinstance(getattr(state, "deal_force_closed", None), set):
        state.deal_force_closed = set()

    # Всегда добавляем как int(deal_id)
    did: int = 0
    try:
        did = int(deal_id)
    except Exception:
        with contextlib.suppress(Exception):
            did = int(str(deal_id).strip())

    if did:
        state.deal_force_closed.add(did)
        logger.info("[lifecycle] hide deal from report: %s", did)
    else:
        logger.debug("[lifecycle] hide deal from report skipped for deal_id=%r", deal_id)


async def finish_if_all_deals_completed() -> None:
    """
    Завершить цикл, когда все игры текущего опроса:
      • зафиксированы (есть в locked_distribution), ИЛИ
      • отмечены как закрытые вручную (state.deal_force_closed), ИЛИ
      • имеют статус SUCCESS (по локальному снапшоту, если доступен).

    После выполнения:
      • await clear_poll_data(state.current_poll_leader or 0)
      • Обновить кнопку в ЛС руководителя обратно на «📋 Создать опрос»
        (используем имеющиеся механизмы без нового API).
      • Лог: [lifecycle] all deals locked/closed → finish cycle
    """
    if not getattr(state, "coordination_cycle_active", False):
        return

    # 1) Текущий набор сделок (поддерживаем как список ID, так и список dict'ов)
    raw_deals = getattr(state, "current_poll_deals", []) or []
    current_ids: List[int] = []
    for it in raw_deals:
        try:
            if isinstance(it, dict):
                did = int(it.get("id", 0) or 0)
            else:
                did = int(it)
            if did:
                current_ids.append(did)
        except Exception:
            continue

    if not current_ids:
        return

    # 2) locked (поддерживаем int и str ключи), closed (пустое множество, если нет)
    raw_locked = getattr(state, "locked_distribution", {}) or {}
    locked: Set[int] = set()
    for k in raw_locked.keys():
        try:
            locked.add(int(k))
        except Exception:
            with contextlib.suppress(Exception):
                locked.add(int(str(k).strip()))

    raw_closed = getattr(state, "deal_force_closed", set()) or set()
    closed: Set[int] = set()
    for k in raw_closed:
        try:
            closed.add(int(k))
        except Exception:
            with contextlib.suppress(Exception):
                closed.add(int(str(k).strip()))

    # Помощник: статус из локального снапшота (если есть)
    def _status_from_snapshot(did: int) -> Optional[int]:
        snap = getattr(state, "current_poll_snapshot", None)
        # dict: {id: {...}|status_id} или {str(id): ...}
        if isinstance(snap, dict):
            val = snap.get(did, snap.get(str(did)))
            if isinstance(val, dict):
                sid = val.get("status_id", val.get("statusId"))
            else:
                sid = val
            try:
                return int(sid) if sid is not None else None
            except Exception:
                return None
        # list[dict]
        if isinstance(snap, list):
            for item in snap:
                if isinstance(item, dict):
                    try:
                        if int(item.get("id", 0) or 0) == did:
                            sid = item.get("status_id", item.get("statusId"))
                            return int(sid) if sid is not None else None
                    except Exception:
                        continue
        return None

    succ_cfg = getattr(settings, "SUCCESSFUL_STATUS_ID", None)

    def _is_success(did: int) -> bool:
        if succ_cfg is None:
            return False
        sid = _status_from_snapshot(did)
        try:
            return sid is not None and int(sid) == int(succ_cfg)
        except Exception:
            return False

    # 3) Условие завершения
    done = all((did in closed) or (did in locked) or _is_success(did) for did in current_ids)

    # 4) Завершение цикла + обновление UI
    if done:
        logger.info("[lifecycle] all deals locked/closed → finish cycle")
        leader_id = getattr(state, "current_poll_leader", 0) or 0
        await clear_poll_data(leader_id)

        # Обновим интерфейс руководителя существующими механизмами
        with contextlib.suppress(Exception):
            await _sync_leader_report(leader_id)   # мягкая синхронизация отчёта/кнопок
        with contextlib.suppress(Exception):
            await _refresh_menu(leader_id)         # если доступно — перерисовать главное меню


# Экспортируем символы, используемые снаружи
__all__ = [
    "_vacuum_old_messages",
    "_is_deal_ready",
    "_check_ready_state",
    "_sync_leader_report",
    "sync_report",
    "create_poll_handler",
    "poll_report_handler",
    "handle_poll_answer",
    "generate_poll_report",
    "_build_report_keyboard",
    "_refresh_detail_views",
    "_send_leader_report",
    "_auto_assign_from_responses",
    "_sv_status",
    "clear_poll_data",
    "hide_deal_from_report",  # ← новый публичный хук
    "finish_if_all_deals_completed",
]

# История изменений [6]:
# 2025-08-31 · добавлен public-hook hide_deal_from_report, выровнено под SSOT.


# ███ [99] _TEST
# --------------------------------------------------------------------

async def _test() -> None:
    """
    Smoke-тест: проверяем _sync_leader_report + «пылесос» поверх заглушек.
    Тест не требует реального Telegram API:
    • Подменяем Bot.get_current() на заглушку с send/edit/delete.
    • Глушим delete_previous_private_messages и vacuum_private.
    • Подменяем generate_poll_report и _build_report_keyboard.
    """
    class _Msg:
        def __init__(self, mid: int) -> None:
            self.message_id = mid

    uid = 777
    # подготовка состояния
    state.current_poll_leader = uid
    state.coordination_cycle_active = True
    state.current_poll_deals = []
    state.responses = {}
    state.last_user_messages.setdefault(uid, [])
    state.last_user_messages[uid] = [_Msg(10), _Msg(11)]
    state.personal_report_message_id = None

    async def _fake_report() -> str:
        return "⚠️ Нет активных опросов."

    def _fake_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[])

    globals()["generate_poll_report"] = _fake_report
    globals()["_build_report_keyboard"] = _fake_kb

    class _DummyBot:
        async def send_message(self, chat_id: int, text: str, parse_mode: Optional[str] = None,
                               reply_markup: Optional[InlineKeyboardMarkup] = None, disable_web_page_preview: Optional[bool] = None):
            class _S:
                message_id = 99
            return _S()

        async def edit_message_text(self, text: str, chat_id: int, message_id: int, parse_mode: Optional[str] = None,
                                    reply_markup: Optional[InlineKeyboardMarkup] = None, disable_web_page_preview: Optional[bool] = None) -> None:
            return None

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            return None

    # Подменяем Bot.get_current() на заглушку
    orig_get = getattr(Bot, "get_current")
    setattr(Bot, "get_current", staticmethod(lambda: _DummyBot()))  # type: ignore[method-assign]

    # Глушим побочные эффекты
    async def _fake_delete(*args: Any, **kwargs: Any) -> None:
        return None

    async def _fake_vacuum(*args: Any, **kwargs: Any) -> None:
        return None

    globals()["delete_previous_private_messages"] = _fake_delete
    globals()["vacuum_private"] = _fake_vacuum

    try:
        await _sync_leader_report()
        assert state.personal_report_message_id == 99
        assert [m.message_id for m in state.last_user_messages[uid]] == [99]
        print("handlers/polls_lifecycle ✅ smoke")
    finally:
        # Восстановим оригинальный резолвер бота
        setattr(Bot, "get_current", orig_get)  # type: ignore[misc]

if __name__ == "__main__":
    import asyncio as _a
    _a.run(_test())

# История изменений:
# 2025-08-29 — усилен dummy-бот (send/edit/delete), добавлен no-op vacuum_private;
#              выровнено под SSOT/фиксы Pylance, стабильная подмена Bot.get_current().
