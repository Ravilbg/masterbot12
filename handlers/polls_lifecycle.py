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
    from typing import Dict, Any, List, Set, Tuple, Optional
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
    state = _StateStub()  # type: ignore

# История изменений: 2025-08-14 — добавлен state.ui_context для фильтрации фоновых перерисовок


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
#   • 2025-08-14 — v6.5: добавлены авто-assign из ответов и approve-кнопки в дашборд.


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
# [0.95] ПЫЛЕСОС ЛИЧНЫХ СООБЩЕНИЙ (фоновая уборка ЛС)
# ════════════════════════════════════════════════════════════════════
"""
Удаляет устаревшие личные сообщения и оставляет только «активные»:
• последние сообщения в ЛС из state.last_user_messages[uid]
• активные detail-блоки из state.detail_blocks[(uid, deal_id)]
• персональный отчёт лидера (если uid == current_poll_leader)
Совместимо с legacy/new сигнатурами delete_previous_private_messages.
"""
_vacuum_log = logger.getChild("vacuum")

async def _vacuum_old_messages() -> None:
    bot = Bot.get_current()

    uids: Set[int] = set()
    try:
        uids.update((getattr(state, "last_user_messages", {}) or {}).keys())
    except Exception:
        pass
    try:
        uids.update((getattr(state, "messages_to_delete", {}) or {}).keys())
    except Exception:
        pass
    try:
        detail_blocks = getattr(state, "detail_blocks", {}) or {}
        uids.update({uid for (uid, _did) in detail_blocks.keys()})
    except Exception:
        detail_blocks = {}

    if not uids:
        return

    leader_uid = getattr(state, "current_poll_leader", None)
    leader_board_id = getattr(state, "personal_report_message_id", None)

    async def _do(uid_: int, keep_: List[Any]) -> None:
        try:
            await delete_previous_private_messages(uid_, keep=keep_)  # новая сигнатура
            return
        except TypeError:
            with contextlib.suppress(Exception):
                await delete_previous_private_messages(bot, uid_, keep=keep_)  # legacy type: ignore

    tasks: List[asyncio.Task] = []
    for uid in uids:
        keep: List[Any] = []
        try:
            keep.extend(((getattr(state, "last_user_messages", {}) or {}).get(uid) or []))
        except Exception:
            pass
        try:
            for (u, _did), msgs in (detail_blocks.items() if isinstance(detail_blocks, dict) else []):
                if u == uid and msgs:
                    keep.extend(msgs)
        except Exception:
            pass
        if leader_uid and uid == leader_uid and leader_board_id:
            keep.append(leader_board_id)
        tasks.append(asyncio.create_task(_do(uid, keep)))

    for r in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(r, Exception):
            _vacuum_log.debug("[vacuum] suppressed deletion error: %s", r)


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
    """
    Толерантный поиск конфигурации ролей из settings.GAME_ROLE_MAPPING.
    Возвращает {"main_leaders": X, "assistants": Y}.
    """
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
    """Парсит uid из значения слота: int | 'uid' | 'Имя Ф.1|uid'."""
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
    """
    if not callable(refresh_deal_details):
        return

    # Определяем набор deal_id для обновления
    if refresh_all:
        impacted = {
            int(d.get("id", 0)) for d in (state.current_poll_deals or []) if int(d.get("id", 0))
        }
    impacted = {int(x) for x in (impacted or set()) if int(x)}

    # Текущие открытые detail-блоки: ключ (uid, deal_id) → список сообщений
    detail_blocks = (getattr(state, "detail_blocks", {}) or {})
    ui_ctx = (getattr(state, "ui_context", {}) or {})

    tasks: List[asyncio.Task] = []
    for (uid, deal_id), _msgs in list(detail_blocks.items()):
        try:
            uid_i, did_i = int(uid), int(deal_id)
        except Exception:
            continue

        # ⛔ Не трогаем пользователей, у кого открыт «Мои игры»
        if ui_ctx.get(uid_i) == "my_games":
            logger.debug("[polls] skip details refresh for uid=%s (context=my_games)", uid_i)
            continue

        if did_i in impacted:
            tasks.append(asyncio.create_task(refresh_deal_details(uid=uid_i, deal_id=did_i)))  # type: ignore

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

# История изменений: 2025-08-14 — добавлена фильтрация по state.ui_context == 'my_games'


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
# ────────────────────────────────────────────────────────────────────
async def _auto_assign_from_responses(impacted: Set[int], apply_to_all_on_admin_flag: bool = False) -> None:
    """
    Строит предварительное распределение по каждой игре из ответов опроса:
      • main_leaders / assistants — из выбранных игр, с учётом «Светофора»;
      • admin — из «🛡️ Админом» (если пакет требует админа).
    Инвариант: 1 пользователь = 1 роль. RED → в расчёт готовности не идёт.

    impacted: набор deal_id, для которых пересчитать.
    apply_to_all_on_admin_flag: если True — пересчитать все текущие игры
      (когда кто-то отметил «🛡️ Админом», это влияет на все игры).
    """
    if apply_to_all_on_admin_flag:
        impacted = {int(d.get("id", 0)) for d in (state.current_poll_deals or []) if int(d.get("id", 0))}
    if not impacted:
        return

    # ранжирование статусов
    def _rank(status: str) -> int:
        # green(0) < yellow(1) < red(2) < ''(3)
        s = (status or "").lower()
        return 0 if s == "green" else 1 if s == "yellow" else 2 if s == "red" else 3

    def _fmt(info: Dict[str, Any]) -> str:
        """Метка вида 'Имя Ф.|uid' — парсится _slot_uid()."""
        fn = (info.get("first_name") or "").strip()
        li = (info.get("last_name_initial") or "").strip()
        uid = int(info.get("uid") or info.get("user_id") or 0)
        base = (f"{fn} {li}." if li else fn).strip() or f"user{uid}"
        return f"{base}|{uid}"

    deals_by_id: Dict[int, Dict[str, Any]] = {int(d.get("id", 0)): d for d in (state.current_poll_deals or [])}

    # общий пул кандидатов в админы
    admin_pool: Dict[int, Dict[str, Any]] = {}
    for pdata in (state.responses or {}).values():
        for adm in (pdata.get("admin_available") or []):
            uid = int(adm.get("user_id", 0))
            if uid:
                admin_pool[uid] = {
                    "uid": uid,
                    "first_name": adm.get("first_name", ""),
                    "last_name_initial": adm.get("last_name_initial", ""),
                    "is_admin_eligible": True,
                }

    for did in set(int(x) for x in impacted if int(x)):
        deal = deals_by_id.get(did)
        if not deal:
            continue
        g_name = str(deal.get("game_name") or deal.get("name") or "")
        pkg = str(deal.get("package") or "")
        need = _role_cfg(g_name)
        need_main = int(need.get("main_leaders", 1))
        need_assist = int(need.get("assistants", 0))
        need_admin = _need_admin_by_package(pkg)

        # пул отметившихся за эту игру
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

        # объединяем признак admin-eligible
        for uid, adm in admin_pool.items():
            if uid in raw_pool:
                raw_pool[uid]["is_admin_eligible"] = True

        # вытянем статусы светофора параллельно
        uids = list(raw_pool.keys())
        async def _one(uid: int) -> Tuple[int, str]:
            return uid, await _sv_status(uid, g_name)
        sv_pairs = await asyncio.gather(*[_one(u) for u in uids], return_exceptions=True)
        sv: Dict[int, str] = {}
        for p in sv_pairs:
            if isinstance(p, Exception):
                continue
            uid, st = p
            sv[uid] = (st or "")

        # кандидаты по приоритетам (green → yellow; red исключаем из core-ролей)
        pool_main = [raw_pool[u] for u in uids if _rank(sv.get(u, "")) in (0, 1)]
        pool_ass  = [raw_pool[u] for u in uids if _rank(sv.get(u, "")) in (0, 1)]
        pool_admin = [raw_pool[u] for u in uids if raw_pool[u].get("is_admin_eligible")]

        key_fn = lambda info: (_rank(sv.get(int(info["uid"]), "")), int(info["uid"]))
        pool_main.sort(key=key_fn)
        pool_ass .sort(key=key_fn)
        pool_admin.sort(key=key_fn)

        used: Set[int] = set()
        dist: Dict[str, Any] = {}

        # lead*
        for i in range(1, need_main + 1):
            pick = next((p for p in pool_main if int(p["uid"]) not in used), None)
            dist[f"lead{i}"] = _fmt(pick) if pick else None
            if pick:
                used.add(int(pick["uid"]))

        # assistant*
        for i in range(1, need_assist + 1):
            pick = next((p for p in pool_ass if int(p["uid"]) not in used), None)
            dist[f"assistant{i}"] = _fmt(pick) if pick else None
            if pick:
                used.add(int(pick["uid"]))

        # admin
        if need_admin:
            pick = next((p for p in pool_admin if int(p["uid"]) not in used), None)
            if not pick:
                pick = next((p for p in pool_ass if int(p["uid"]) not in used), None)  # fallback
            dist["admin"] = _fmt(pick) if pick else None
            if pick:
                used.add(int(pick["uid"]))
        else:
            dist["admin"] = None

        if not getattr(state, "distribution_cache", None):
            state.distribution_cache = {}
        state.distribution_cache[str(did)] = dist

# ════════════════════════════════════════════════════════════════════
# [2.6] CHAT RESOLVER (куда слать сервисные уведомления)
# ════════════════════════════════════════════════════════════════════
async def _resolve_notify_chat_id() -> Optional[int]:
    """
    Возвращает первый доступный чат для сервисных сообщений.
    ВАЖНО: приоритет на admin_chat_id, т.к. он гарантированно рабочий в тесте.
    Порядок: state.admin_chat_id → POLLS_CHAT_ID → LEADERS_CHAT_ID → ADMIN_CHAT_ID.
    Без падений, строки/числа приводим к int.
    """
    candidates = [
        getattr(state, "admin_chat_id", None),          # ← приоритетно
        getattr(settings, "POLLS_CHAT_ID", None),
        getattr(settings, "LEADERS_CHAT_ID", None),
        getattr(settings, "ADMIN_CHAT_ID", None),
    ]
    for cid in candidates:
        if cid is None:
            continue
        try:
            return int(str(cid).strip())
        except Exception:
            continue
    return None

# История изменений: 2025-08-15 — приоритет admin_chat_id; безопасное приведение к int


# ════════════════════════════════════════════════════════════════════
# [2.7] SWAP / DISTRIBUTION HELPERS
# ════════════════════════════════════════════════════════════════════
async def _short_label(uid: int) -> str:
    """«Имя Ф.» для слотов и уведомлений."""
    ui = await get_user_info(uid) or {}
    fn = (ui.get("first_name") or "").strip()
    li = (ui.get("last_name_initial") or "").strip()
    return f"{fn} {li}".strip() if (fn or li) else f"user{uid}"

def _slot_label(uid: int, base: Optional[str] = None) -> str:
    """Метка слота: 'Имя Ф.|uid'."""
    return f"{(base or '').strip() or 'user'+str(uid)}|{uid}"

def _remove_uid_from_dist(dist: Dict[str, Any], uid: int) -> None:
    """Инвариант «1 пользователь = 1 роль» — убираем uid из всех lead*/assistant*/admin."""
    for k, v in list(dist.items()):
        if not isinstance(k, str):
            continue
        if k.startswith("lead") or k.startswith("assistant") or k == "admin":
            if isinstance(v, str) and v.rsplit("|", 1)[-1].isdigit() and int(v.rsplit("|", 1)[-1]) == uid:
                dist[k] = None

def _ensure_role_slots(dist: Dict[str, Any], game_name: str, package: str) -> Tuple[int, int, int]:
    """Создаёт недостающие ключи lead{i}/assistant{i}/admin согласно конфигурации."""
    need = _role_cfg(game_name)
    need_main = int(need.get("main_leaders", 1))
    need_assist = int(need.get("assistants", 0))
    need_admin = _need_admin_by_package(package)
    for i in range(1, max(1, need_main) + 1):
        dist.setdefault(f"lead{i}", None)
    for i in range(1, max(0, need_assist) + 1):
        dist.setdefault(f"assistant{i}", None)
    dist.setdefault("admin", None if need_admin else None)
    return need_main, need_assist, need_admin

def _first_empty_slot(dist: Dict[str, Any], prefix: str, count: int) -> Optional[str]:
    """Возвращает имя первого пустого слота с данным префиксом (lead/assistant)."""
    for i in range(1, count + 1):
        key = f"{prefix}{i}"
        if dist.get(key) in (None, "", 0):
            return key
    return f"{prefix}1" if count >= 1 else None

def _names_list_from_dist(dist: Dict[str, Any], prefix: str, count_guess: int) -> List[str]:
    """Имена без |uid из слотов prefix* (для уведомления)."""
    out: List[str] = []
    for i in range(1, max(1, count_guess) + 1):
        v = dist.get(f"{prefix}{i}")
        if isinstance(v, str):
            out.append(v.split("|", 1)[0])
    return [x for x in out if x]

def _team_summary_text(dist: Dict[str, Any], game_name: str, package: str) -> str:
    """Человекочитаемый состав команды одной строкой + переносы."""
    need_main, need_assist, _need_admin = _ensure_role_slots(dist, game_name, package)
    mains = ", ".join(_names_list_from_dist(dist, "lead", need_main)) or "—"
    assists = ", ".join(_names_list_from_dist(dist, "assistant", need_assist)) or "—"
    adm = dist.get("admin")
    admin = (adm.split("|", 1)[0] if isinstance(adm, str) else "") or "—"
    trainee = dist.get("trainee")
    trainee_s = (trainee.split("|", 1)[0] if isinstance(trainee, str) else "")
    parts = [f"🧭 Ведущие: {mains}", f"🛟 Помощники: {assists}", f"🛡️ Админ: {admin}"]
    if trainee_s:
        parts.append(f"🎓 Стажёр: {trainee_s}")
    return "\n".join(parts)

async def _insert_candidate_into_distribution(deal_id: int, role: str, uid: int, status: str) -> Dict[str, Any]:
    """
    Точечно встраивает кандидата в distribution_cache[str(deal_id)]:
    • green/yellow → целевая роль;
    • red → слот trainee (не учитывается в готовности).
    Инвариант: 1 пользователь = 1 роль.
    Возвращает актуализированный dist.
    """
    # найдём сделку
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == int(deal_id)), {}) or {}
    game_name = str(deal.get("game_name") or deal.get("name") or "")
    package = str(deal.get("package") or "")
    # получить/подготовить dist
    if not getattr(state, "distribution_cache", None):
        state.distribution_cache = {}
    dist: Dict[str, Any] = dict((state.distribution_cache or {}).get(str(deal_id)) or {})
    need_main, need_assist, _need_admin = _ensure_role_slots(dist, game_name, package)

    # удалить кандидата из всех ролей (если вдруг уже был)
    _remove_uid_from_dist(dist, uid)

    # метка
    label = _slot_label(uid, await _short_label(uid))

    if status == "red" and role in {"main", "assist"}:
        # «красный» — в стажёры, готовность не увеличивает
        dist["trainee"] = label
    else:
        if role == "main":
            key = _first_empty_slot(dist, "lead", need_main) or "lead1"
            dist[key] = label
        elif role == "assist":
            key = _first_empty_slot(dist, "assistant", need_assist) or "assistant1"
            dist[key] = label
        elif role == "admin":
            dist["admin"] = label
        else:
            # защитный фолбэк — как помощник
            key = _first_empty_slot(dist, "assistant", max(1, need_assist)) or "assistant1"
            dist[key] = label

    state.distribution_cache[str(deal_id)] = dist
    return dist
def _assigned_role_from_state(uid: int, deal_id: int) -> Optional[str]:
    """
    Возвращает роль ('main'|'assist'|'admin') пользователя по сделке из
    locked_distribution (в приоритете) или distribution_cache.
    """
    uid = int(uid)
    did_s = str(int(deal_id))
    # 1) lock (утверждённый состав)
    dist = (getattr(state, "locked_distribution", {}) or {}).get(int(deal_id)) or {}
    # 2) cache (предварительный состав)
    if not dist:
        dist = (getattr(state, "distribution_cache", {}) or {}).get(did_s) or {}
    if not isinstance(dist, dict):
        return None

    def _match(val: Any) -> bool:
        if isinstance(val, int):
            return val == uid
        if isinstance(val, str) and "|" in val:
            tail = val.rsplit("|", 1)[-1]
            return tail.isdigit() and int(tail) == uid
        return False

    # main
    for k, v in dist.items():
        if isinstance(k, str) and k.startswith("lead") and _match(v):
            return "main"
    # assist
    for k, v in dist.items():
        if isinstance(k, str) and k.startswith("assistant") and _match(v):
            return "assist"
    # admin
    if _match(dist.get("admin")):
        return "admin"
    return None


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
    # запасной путь — запросить список актуальных сделок и найти там
    try:
        deals = await get_amocrm_deals()
        for d in deals or []:
            if int(d.get("id", 0)) == did:
                return d
    except Exception:
        pass
    return {}


# ════════════════════════════════════════════════════════════════════
# [3] СОЗДАНИЕ ОПРОСА
# ════════════════════════════════════════════════════════════════════
@router.message(Command("create_poll"))
@router.message(lambda m: (m.text or "") == "📋 Создать опрос")
async def create_poll_handler(message: types.Message) -> None:
    uid = message.from_user.id
    logger.info("[create_poll] invoked by %d", uid)

    # 1) доступ
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in getattr(settings, "ACCESS", {}).get("poll", ["admin", "leader"]):
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # активный цикл → просто отчёт
    if state.coordination_cycle_active:
        await message.answer("⚠️ Уже есть активный опрос.", reply_markup=await get_main_menu(uid))
        with contextlib.suppress(Exception):
            await _refresh_menu(uid)
        await _delete_trigger(message)
        return

    if not state.admin_chat_id:
        await message.answer("⚠️ Чат не настроен.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # 2) загрузка сделок
    try:
        deals = await get_amocrm_deals()
    except Exception as e:
        logger.exception("[create_poll] get_amocrm_deals failed: %s", e)
        await message.answer("⚠️ Не удалось получить игры из AmoCRM.")
        await _delete_trigger(message)
        return

    now, window = datetime.now(tz=MSK_TZ), datetime.now(tz=MSK_TZ) + timedelta(days=14)
    poll_deals: List[Dict[str, Any]] = [
        d for d in (deals or [])
        if d.get("status_id") in getattr(settings, "NEW_GAMES_STATUS_IDS", [])
        and isinstance(d.get("event_datetime"), datetime)
        and now <= d["event_datetime"] <= window
        and not d.get("team_leads")
    ]
    if not poll_deals:
        await message.answer("😔 Нет новых игр.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # 3) инициализация state
    state.current_poll_deals        = poll_deals
    state.current_poll_leader       = uid
    state.responses.clear()
    state.distribution_cache.clear()
    state.poll_distribution.clear()
    state.deal_force_closed.clear()
    state.confirmed_users.clear() if hasattr(state, "confirmed_users") else None
    state.current_deal_ready.clear()
    state.all_ready_notified        = False
    state.personal_report_message_id = None
    state.coordination_cycle_active = True
    state.force_closed              = False

    with contextlib.suppress(Exception):
        await _refresh_menu(uid)

    # 4) публикация опросов (по 8 опций + 2 служебных)
    def _deal_dt_parts(d: Dict[str, Any]) -> Tuple[str, str]:
        """Дата всегда из event_datetime/строк, время — ПРЕЖДЕ ВСЕГО из кастомного event_time."""
        dt = d.get("event_datetime")
        # дата
        date_s = dt.strftime("%d.%m") if isinstance(dt, datetime) else str(d.get("event_date") or "—")
        # время: приоритет у кастомного поля
        et = str(d.get("event_time") or "").strip()
        if et:
            time_s = et
        elif isinstance(dt, datetime):
            time_s = dt.strftime("%H:%M")
        else:
            time_s = str(d.get("event_time") or "—")
        return date_s, time_s

    def _deal_extras(d: Dict[str, Any]) -> Tuple[str, str]:
        pkg = str(d.get("package") or "").strip()
        # поддерживаем несколько возможных ключей для «бонусов»
        bonuses = d.get("bonuses")
        if bonuses is None:
            bonuses = d.get("bonus")
        if bonuses is None:
            bonuses = d.get("extra_bonuses")
        if bonuses is None:
            bonuses = d.get("extra_services")
        return pkg, str(bonuses or "").strip()

    urgent = any(d["event_datetime"] <= now + timedelta(days=3) for d in poll_deals)
    header_base = "🚨 Срочные!" if urgent else "📊 Новые игры"
    chunks = [poll_deals[i:i + 8] for i in range(0, len(poll_deals), 8)]
    for idx, chunk in enumerate(chunks, 1):
        header = f"{header_base} (Часть {idx})" if len(chunks) > 1 else header_base
        opts: List[str] = []
        idx_map: Dict[int, int] = {}

        for i, d in enumerate(chunk):
            title = _deal_title(d)
            date_s, time_s = _deal_dt_parts(d)
            pkg_s, bonus_s = _deal_extras(d)

            # Формат: "🎉 Игра — дата время пакет бонусы"
            parts: List[str] = [f"🎉 {title} — {date_s} {time_s}"]
            if pkg_s:
                parts.append(pkg_s)
            if bonus_s:
                parts.append(bonus_s)

            opts.append(truncate(" ".join(parts)))
            idx_map[i] = int(d["id"])

        # служебные опции
        opts += ["🚫 Не смогу работать", "🛡️ Могу Администратором"]

        poll = await Bot.get_current().send_poll(
            state.admin_chat_id,
            header,
            opts,
            is_anonymous=False,
            allows_multiple_answers=True,
        )
        state.responses[poll.poll.id] = {
            "deals": {int(d["id"]): [] for d in chunk},
            "not_available": [],
            "admin_available": [],
            "deal_indices": idx_map,
        }
        logger.debug("[create_poll] poll sent: id=%s, deals=%s", poll.poll.id, list(idx_map.values()))

        # было:
    # await message.answer("✅ Опросы отправлены.")
    # with contextlib.suppress(Exception):
    #     await _refresh_menu(uid)

    sent_info = await message.answer("✅ Опросы отправлены.")
    # ✨ Добавим одноразовое сообщение в список на удаление, чтобы «пылесос» его снёс
    try:
        if not getattr(state, "messages_to_delete", None):
            state.messages_to_delete = {}
        state.messages_to_delete.setdefault(uid, [])
        mid = getattr(sent_info, "message_id", None)
        if mid:
            state.messages_to_delete[uid].append(int(mid))
    except Exception:
        pass

    with contextlib.suppress(Exception):
        await _refresh_menu(uid)


# ════════════════════════════════════════════════════════════════════
# [4] ОТЧЁТ ЛИДЕРУ / ПРИЁМ ОТВЕТОВ
# ════════════════════════════════════════════════════════════════════
def _merge_keyboards(k1: InlineKeyboardMarkup, k2: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*(k1.inline_keyboard or []), *(k2.inline_keyboard or [])])

async def generate_poll_report() -> str:
    """Короткий заголовок отчёта; галочки/кнопки — в клавиатуре."""
    return "📊 Выберите игру, чтобы открыть детали."

def _counts_ready_for_deal(deal: Dict[str, Any]) -> Tuple[bool, Dict[str, int]]:
    """
    Считает готовность по state.distribution_cache[str(deal_id)] c защитой от дублей.
    Правило «1 пользователь = 1 роль»:

    • считаем уникальные uid в main/assist;
    • admin обязателен при пакете (стандарт/стандарт+/премиум/VIP/биглион).
    """
    did = int(deal.get("id") or 0)
    g_name = str(deal.get("game_name") or deal.get("name") or "")
    pkg = str(deal.get("package") or "")
    cfg = _role_cfg(g_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))
    need_admin = _need_admin_by_package(pkg)

    dist: Dict[str, Any] = (getattr(state, "distribution_cache", {}) or {}).get(str(did), {}) or {}

    # собираем uid по ролям
    main_uids = [_slot_uid(dist.get(f"lead{i}")) for i in range(1, max(1, need_main) + 1)]
    assist_uids = [_slot_uid(dist.get(f"assistant{i}")) for i in range(1, max(0, need_assist) + 1)]
    admin_uid = _slot_uid(dist.get("admin"))

    # инварианта: 1 пользователь = 1 роль → считаем по множествам и вычитаем пересечения
    ms = [u for u in main_uids if u]
    as_ = [u for u in assist_uids if u]
    as_uniq = [u for u in as_ if u not in ms]
    have_main = len(set(ms))
    have_assist = len(set(as_uniq))
    have_admin = 1 if admin_uid and admin_uid not in ms and admin_uid not in as_ else (1 if (admin_uid and need_admin == 0) else 0)

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
    Внизу: «Утвердить все», если есть готовые незалоченные.
    """
    rows: List[List[InlineKeyboardButton]] = []
    any_ready_unlocked = False
    locked_map = (getattr(state, "locked_distribution", {}) or {})

    for d in (state.current_poll_deals or []):
        did = int(d.get("id") or 0)
        if not did or did in (state.deal_force_closed or set()):
            continue

        ready, _ = _counts_ready_for_deal(d)
        name = str(d.get("game_name") or d.get("name") or "Игра")
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

    # actions-вставка из polls_distribution (если доступна)
    try:
        from handlers.polls_distribution import distribution_actions_markup  # lazy import
        actions = distribution_actions_markup()
        rows.extend(actions.inline_keyboard or [])
    except Exception:
        pass

    return InlineKeyboardMarkup(inline_keyboard=rows if rows else [[InlineKeyboardButton(text="Обновить", callback_data="poll_back_to_games_list")]])

async def _send_leader_report(leader_id: int) -> None:
    bot = Bot.get_current()
    text = await generate_poll_report()
    kb = _build_report_keyboard()

    # подчистка старых сообщений у лидера
    with contextlib.suppress(TypeError):
        await delete_previous_private_messages(bot, leader_id, keep=[])
    with contextlib.suppress(Exception):
        await delete_previous_private_messages(leader_id, keep=[])  # новая сигнатура

    sent = await bot.send_message(leader_id, text, parse_mode="Markdown", reply_markup=kb)
    state.personal_report_message_id = sent.message_id
    state.last_user_messages[leader_id] = [sent]

async def _sync_leader_report(leader_id: Optional[int] = None) -> None:
    """Обратная совместимость: обновить отчёт лидеру (старый коллбэк)."""
    lid = leader_id or getattr(state, "current_poll_leader", None)
    if not lid:
        with contextlib.suppress(Exception):
            uids = await get_all_leader_uids()
            lid = (uids or [None])[0]
    if not lid:
        logger.debug("[sync_report] leader_id undefined — skip")
        return
    await _send_leader_report(int(lid))

# Алиас для внешних вызовов (polls_distribution._try_sync_report)
async def sync_report() -> None:
    await _sync_leader_report()

@router.message(lambda m: (m.text or "") == "📊 Отчёт по опросу")
async def poll_report_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in getattr(settings, "ACCESS", {}).get("poll", ["admin", "leader"]):
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return
    if not state.coordination_cycle_active:
        await message.answer("⚠️ Нет активных опросов.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    bot = Bot.get_current()
    with contextlib.suppress(TypeError):
        await delete_previous_private_messages(bot, uid, keep=[])
    with contextlib.suppress(Exception):
        await delete_previous_private_messages(uid, keep=[])

    dash = await bot.send_message(uid, await generate_poll_report(), parse_mode="Markdown", reply_markup=_build_report_keyboard())
    state.personal_report_message_id = dash.message_id
    state.last_user_messages[uid] = [dash]

    with contextlib.suppress(Exception):
        await _refresh_menu(uid)
    await _delete_trigger(message)

@router.poll_answer()
async def handle_poll_answer(event: types.PollAnswer) -> None:
    """Фиксируем выборы пользователя, автораспределяем и пересчитываем готовность."""
    uid, poll_id, chosen = event.user.id, event.poll_id, (event.option_ids or [])
    data = (state.responses or {}).get(poll_id)
    if not data:
        return

    logger.debug("[answer] uid=%d poll=%s choices=%s", uid, poll_id, chosen)

    # 1) очистим следы прежних ответов этого uid
    for lst in (data.get("deals") or {}).values():
        lst[:] = [u for u in (lst or []) if int(u.get("user_id", 0)) != uid]
    data["not_available"][:] = [u for u in (data.get("not_available") or []) if int(u.get("user_id", 0)) != uid]
    data["admin_available"][:] = [u for u in (data.get("admin_available") or []) if int(u.get("user_id", 0)) != uid]

    # 2) запишем новые
    ui = await get_user_info(uid) or {}
    base = {
        "user_id": uid,
        "first_name": ui.get("first_name", ""),
        "last_name_initial": ui.get("last_name_initial", ""),
        "is_admin_eligible": False,
    }
    num = len(data.get("deal_indices") or {})
    impacted: Set[int] = set()
    admin_flag = False

    for idx in chosen:
        if idx < num:
            did = int((data["deal_indices"] or {})[idx])
            if did not in (state.deal_force_closed or set()):
                (data["deals"][did]).append(base.copy())
                impacted.add(did)
        elif idx == num:
            (data["not_available"]).append(base.copy())
        else:
            adm = base.copy()
            adm["is_admin_eligible"] = True
            (data["admin_available"]).append(adm)
            admin_flag = True  # влияет на ВСЕ игры

    # 3) автораспределение (сразу обновит distribution_cache)
    await _auto_assign_from_responses(impacted=impacted, apply_to_all_on_admin_flag=admin_flag)

    # 4) эффекты UI/индикации
    await _sync_leader_report()
    scope = impacted if not admin_flag else {int(d.get("id", 0)) for d in (state.current_poll_deals or [])}
    await _check_ready_state(scope)
    asyncio.create_task(_refresh_detail_views(scope, refresh_all=admin_flag))
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

    # куда публикуем
    chat_id = await _resolve_notify_chat_id()
    if not chat_id:
        await callback.answer("⚠️ Не настроен чат для объявлений.", show_alert=True)
        logger.error("[swap] no available chat for notify; uid=%s deal=%s", uid, deal_id)
        return

    # объявление об открытой замене + кнопка отклика
    short = await _short_label(uid)
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

    bot = Bot.get_current()

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
            await bot.send_message(uid, "⚠️ Не удалось отправить объявление в чат. "
                                        "Я сохранил запрос на замену. "
                                        "Скинь этот текст в рабочий чат вручную:\n\n" + text)
        except Exception:
            pass

    # CRM: убрать подтверждающие теги пользователя и вернуть сделку в «Бронь»
    try:
        from services.amocrm import revert_to_bron_after_swap  # lazy import
        await revert_to_bron_after_swap(int(deal_id), uid=uid, short_base=await _short_label(uid))
    except Exception as e:
        logger.warning("[swap] CRM revert failed for deal=%s: %s", deal_id, e)

    # UI: сообщим пользователю и мягко обновим отчёты/детали
    with contextlib.suppress(Exception):
        await callback.answer("Запрос на замену отправлен.", show_alert=False)
    await _sync_leader_report()
    asyncio.create_task(_refresh_detail_views({int(deal_id)}, refresh_all=False))

# История изменений: 2025-08-15 — логи, приоритет admin_chat, фолбэк в ЛС, обязательный ACK

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

    # «первый клик выигрывает»
    accepted = req.get("accepted_by")
    if accepted is not None:
        await callback.answer("Уже занято — замена назначена.", show_alert=True)
        logger.info("[swap] accept: already taken by uid=%s", accepted)
        return

    # фиксируем победителя
    req["accepted_by"] = uid

    # проверка по «Светофору»
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == int(deal_id)), {}) or {}
    game_name = str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal_id}")
    status = await _sv_status(uid, game_name)  # '' | green | yellow | red

    # точечная пересборка распределения под кандидата
    dist = await _insert_candidate_into_distribution(deal_id=deal_id, role=role, uid=uid, status=status)

    # уведомление в чат
    bot = Bot.get_current()
    chat_id = await _resolve_notify_chat_id()
    if chat_id:
        try:
            pkg = str(deal.get("package") or "")
            summary = _team_summary_text(dist, game_name, pkg)
            title = _deal_title(deal)
            dt = deal.get("event_datetime")
            date_s = dt.strftime("%d.%m") if hasattr(dt, "strftime") else str(deal.get("event_date") or "—")
            time_s = str(deal.get("event_time") or "—")

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

# История изменений: 2025-08-15 — логи, безопасные уведомления, стабильный ACK

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

async def finish_if_all_deals_completed() -> None:
    """
    Если все игры цикла уже «утверждены» (есть в locked_distribution),
    завершаем цикл. Идемпотентно.
    """
    if not state.coordination_cycle_active:
        return
    deals = [int(d.get("id", 0) or 0) for d in (state.current_poll_deals or [])]
    locked = set((getattr(state, "locked_distribution", {}) or {}).keys())
    if deals and all(d in locked for d in deals):
        logger.info("[lifecycle] all deals locked → finish cycle")
        await clear_poll_data(getattr(state, "current_poll_leader", 0) or 0)


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
    "finish_if_all_deals_completed",
]


# ███ [99] _TEST
# --------------------------------------------------------------------
async def _test() -> None:
    """
    Smoke-тест: проверяем _sync_leader_report + «пылесос» поверх заглушек.
    """
    class _Msg:
        def __init__(self, mid: int) -> None:
            self.message_id = mid

    uid = 777
    state.current_poll_leader = uid
    state.coordination_cycle_active = True
    state.current_poll_deals = []
    state.responses = {}
    state.last_user_messages[uid] = [_Msg(10), _Msg(11)]
    state.personal_report_message_id = None

    async def _fake_report() -> str: return "⚠️ Нет активных опросов."
    def _fake_kb() -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[])
    globals()["generate_poll_report"] = _fake_report
    globals()["_build_report_keyboard"] = _fake_kb

    class _Bot:
        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            class _S: message_id = 99
            return _S()
    Bot.set_current(_Bot())  # type: ignore

    async def _fake_delete(*args, **kwargs): return None
    globals()["delete_previous_private_messages"] = _fake_delete

    await _sync_leader_report()
    assert state.personal_report_message_id == 99
    assert [m.message_id for m in state.last_user_messages[uid]] == [99]
    print("handlers/polls_lifecycle ✅ smoke")

if __name__ == "__main__":
    import asyncio as _a
    _a.run(_test())
