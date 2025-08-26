# handlers/polls_distribution.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Ручное управление распределением (этап лидера).
После «Утвердить» распределение фиксируется и запускается цикл подтверждений.

Версия v14.9-cycle • 2025-08-12
──────────────────────────────────────────────────────────────────────────────
• Единый коллбэк «Утвердить»: poll_approve_{deal_id}.
• Источник правды по составу — state.distribution_cache / poll_details.distribution.
• Автораспределение main/assist из ответов опроса + Светофор; офлайн-фолбэк.
• Поддержка legacy-ключей main_leaders/assistants.
• Уведомление уходит в POLLS_CHAT_ID / LEADERS_CHAT_ID / ADMIN_CHAT_ID.
• В уведомлении рабочая кнопка «🎲 Личный кабинет» (deep-link /start=my_games).
• Идемпотентность «Утвердить»: повторный клик не дублирует фиксацию/уведомления.
• Перерисовка «Мои игры» коалесцируется (один редрав на батч uid).
"""


# ════════════════════════════════════════════════════════════════════
# [0] IMPORTS
# ════════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Set, Tuple, Optional

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from core.config import settings
from core.state import state
import handlers.polls_lifecycle as plc  # локальный импорт, чтобы избежать циклов

logger = logging.getLogger(__name__)
router = Router(name="polls_distribution")

# алиас на проверку готовности сделки (из lifecycle)
_is_deal_ready = plc._is_deal_ready

# SSOT-резолвер уведомительного чата: awaitable-обёртка с безопасной передачей bot
from core.utils import resolve_notify_chat_id as _ssot_resolve_notify_chat_id
async def _resolve_notify_chat_id(bot: Any = None) -> Optional[int]:
    try:
        # основная сигнатура (c bot)
        return _ssot_resolve_notify_chat_id(bot)
    except Exception:
        try:
            # совместимость со старыми версиями (без bot)
            return _ssot_resolve_notify_chat_id()
        except Exception:
            return None


async def _try_sync_report() -> None:
    """Мягкая перерисовка отчёта после «Утвердить» (если функция есть)."""
    try:
        fn = getattr(plc, "sync_report", None)
        if callable(fn):
            await fn()
    except Exception as e:
        logger.warning("[polls_dist] sync_report skipped: %s", e)

# История изменений: [0] 2025-08-24 — убран прямой импорт services.gsheets; shim резолвера передаёт bot.

# ════════════════════════════════════════════════════════════════════
# [1.m] ПРИОРИТЕТЫ (AmoCRM): кэш месячных счётчиков по тегам
# ════════════════════════════════════════════════════════════════════
from typing import Dict

async def _load_monthly_role_counters(force: bool = False) -> Dict[int, int]:
    if not force:
        cached = getattr(state, "monthly_role_counters", None)
        if isinstance(cached, dict):
            try:
                return {int(k): int(v) for k, v in cached.items()}
            except Exception:
                pass
    try:
        from services.amocrm import get_monthly_role_tag_counters  # type: ignore
    except Exception:
        async def get_monthly_role_tag_counters(*_a, **_kw) -> Dict[int, int]:  # type: ignore
            return {}
    try:
        res = get_monthly_role_tag_counters()
        data = await res if asyncio.iscoroutine(res) else res  # type: ignore
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:
        logger.debug("[priority] monthly counters load failed: %s", exc)
        data = {}
    try:
        norm = {int(k): int(v) for k, v in (data or {}).items()}
    except Exception:
        norm = {}
    setattr(state, "monthly_role_counters", norm)
    return norm

async def _get_monthly_counters() -> Dict[int, int]:
    cached = getattr(state, "monthly_role_counters", None)
    if isinstance(cached, dict):
        try:
            return {int(k): int(v) for k, v in cached.items()}
        except Exception:
            pass
    return await _load_monthly_role_counters(force=False)


# ════════════════════════════════════════════════════════════════════
# [1] УТИЛИТЫ: нормализация, ЕДИНЫЙ КЭШ, commit в state, формат уведомлений
# ════════════════════════════════════════════════════════════════════
"""
Назначение:
• ЕДИНЫЙ источник правды по составу — state.distribution_cache[str(deal_id)].
• Мягкая миграция из зеркал (poll_details.distribution / poll_distribution) в единый кэш.
• Инвариант «1 uid → 1 роль» (main > assist > admin) на чтение и запись.
• Сбор «слотов» под «Мои игры»: lead1/assistant1/admin/trainee «Имя Ф.<суффикс>|uid».
• Запись утверждённого состава делает блок [1.k]; здесь — только нормализация/чтение.
• НОВОЕ: приоритизация подбора кандидатов по «месячным счётчикам тегов» из AmoCRM.
"""

import asyncio
import re
from typing import Any, Dict, List, Set, Tuple, Optional
from contextlib import suppress

# — мягкий импорт state (во избежание жалоб Pylance и циклических импортов)
try:  # предпочитаемый путь проекта
    from core.state import state  # type: ignore
except Exception:
    try:
        from state import state  # type: ignore
    except Exception:
        state = object()  # type: ignore[assignment]

# SSOT-утилиты
from core.utils import (
    parse_uid,          # str "Имя Ф.|123" → 123
    to_uid_list,        # Any → List[int]
    normalize_roles,    # dict со слотами/ролями → {'main': [], 'assist': [], 'admin': []}
    team_bulleted_lines,
)

# Алиасы для совместимости с остальным кодом модуля
_parse_uid = parse_uid
_as_user_list = to_uid_list
_normalize_roles = normalize_roles


def _ensure_state_structs() -> None:
    """
    Гарантирует наличие требуемых структур в state с корректными типами.
    """
    if not hasattr(state, "assigned_index") or not isinstance(getattr(state, "assigned_index", None), dict):
        state.assigned_index = {}            # type: ignore[attr-defined]
    if not hasattr(state, "locked_distribution") or not isinstance(getattr(state, "locked_distribution", None), dict):
        state.locked_distribution = {}       # type: ignore[attr-defined]
    if not hasattr(state, "pending_confirmations") or not isinstance(getattr(state, "pending_confirmations", None), dict):
        state.pending_confirmations = {}     # type: ignore[attr-defined]
    if not hasattr(state, "distribution_cache") or not isinstance(getattr(state, "distribution_cache", None), dict):
        state.distribution_cache = {}        # type: ignore[attr-defined]
    if not hasattr(state, "poll_details") or not isinstance(getattr(state, "poll_details", None), dict):
        state.poll_details = {}              # type: ignore[attr-defined]
    if not hasattr(state, "poll_distribution") or not isinstance(getattr(state, "poll_distribution", None), dict):
        state.poll_distribution = {}         # type: ignore[attr-defined]
    if not hasattr(state, "current_poll_deals") or not isinstance(getattr(state, "current_poll_deals", None), list):
        state.current_poll_deals = []        # type: ignore[attr-defined]
    if not hasattr(state, "responses") or not isinstance(getattr(state, "responses", None), dict):
        state.responses = {}                 # type: ignore[attr-defined]
    if not hasattr(state, "monthly_role_counters") or not isinstance(getattr(state, "monthly_role_counters", None), dict):
        state.monthly_role_counters = {}     # type: ignore[attr-defined]


def _dedupe_roles(roles: Dict[str, List[int]]) -> Dict[str, List[int]]:
    """
    Жёстко гарантирует инвариант «один uid → одна роль» в приоритете main > assist > admin.
    """
    seen: Set[int] = set()
    out: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}

    for u in roles.get("main", []) or []:
        if isinstance(u, int) and u not in seen:
            out["main"].append(u)
            seen.add(u)

    for u in roles.get("assist", []) or []:
        if isinstance(u, int) and u not in seen:
            out["assist"].append(u)
            seen.add(u)

    for u in roles.get("admin", []) or []:
        if isinstance(u, int) and u not in seen:
            out["admin"].append(u)
            seen.add(u)

    return out


def _uids_from_roles(roles: Dict[str, List[int]]) -> Set[int]:
    """
    Возвращает множество всех uid, задействованных в ролях.
    """
    return set(roles.get("main", []) or []) | set(roles.get("assist", []) or []) | set(roles.get("admin", []) or [])


def _dedupe_slots(slots: Dict[str, Any]) -> Dict[str, Any]:
    """
    Дедупликация слотов по инварианту «1 пользователь = 1 роль» с приоритетом:
    lead* > assistant* > admin. trainee НЕ участвует в счётчике и остаётся как есть.
    Если пользователь уже занят на более приоритетной роли, более низкоприоритетный слот очищается.
    """
    if not isinstance(slots, dict) or not slots:
        return {}

    def _uid_from_slot_value(v: Any) -> Optional[int]:
        if isinstance(v, str):
            return _parse_uid(v)
        if isinstance(v, (list, tuple)):
            for item in v:
                u = _parse_uid(item)
                if u is not None:
                    return u
        if isinstance(v, int):
            return v
        return None

    # Приоритетные группы ключей
    lead_keys = sorted([k for k in slots if isinstance(k, str) and k.startswith("lead")],
                       key=lambda k: int(re.search(r"(\d+)$", k).group(1)) if re.search(r"(\d+)$", k) else 0)
    asst_keys = sorted([k for k in slots if isinstance(k, str) and k.startswith("assistant")],
                       key=lambda k: int(re.search(r"(\d+)$", k).group(1)) if re.search(r"(\d+)$", k) else 0)
    admin_keys = ["admin"] if "admin" in slots else []

    used: Set[int] = set()
    out = dict(slots)

    for group in (lead_keys, asst_keys, admin_keys):
        for key in group:
            uid = _uid_from_slot_value(out.get(key))
            if uid is None:
                continue
            if uid in used:
                out[key] = ""
            else:
                used.add(uid)

    return out


# ────────────────────────────────────────────────────────────────────
# ЕДИНЫЙ КЭШ: только state.distribution_cache[str(deal_id)]
# + мягкая миграция из зеркал при первом обращении
# ────────────────────────────────────────────────────────────────────
def _slots_from_roles_placeholder(roles: Dict[str, List[int]], *, keep_admin_many: bool = False) -> Dict[str, Any]:
    """
    Формирует слотовую карту из ролей с ПЛЕЙСХОЛДЕРАМИ имени:
      lead{i}: "uid:{uid}|{uid}", assistant{i}: "uid:{uid}|{uid}", admin: "uid:{uid}|{uid}"
    Имена восстановятся позже нормализаторами (детали/коммит).
    """
    slots: Dict[str, Any] = {}
    for i, uid in enumerate(roles.get("main", []) or [], start=1):
        slots[f"lead{i}"] = f"uid:{uid}|{uid}"
    for i, uid in enumerate(roles.get("assist", []) or [], start=1):
        slots[f"assistant{i}"] = f"uid:{uid}|{uid}"
    admins = roles.get("admin", []) or []
    if admins:
        slots["admin"] = f"uid:{admins[0]}|{admins[0]}"
    return slots


def _migrate_from_mirrors_to_single_cache(deal_id: int) -> Optional[Dict[str, List[int]]]:
    """
    Приводит распределение к ЕДИНОМУ кэшу state.distribution_cache[str(deal_id)].
    Логика приоритета:
      1) Если есть «детали» (poll_details.distribution) и они полнее/новее — берём их.
      2) Иначе, если единый кэш непустой — он источник правды.
      3) Иначе пробуем legacy (poll_distribution).
    Всегда синхронизируем зеркала на один и тот же вид (слотовая схема).
    Возвращает роли (dedupe) или None, если вообще ничего не найдено.
    """
    _ensure_state_structs()
    did = int(deal_id)
    did_s = str(did)

    dc_all = getattr(state, "distribution_cache", {}) or {}
    details_all = getattr(state, "poll_details", {}) or {}
    legacy_all = getattr(state, "poll_distribution", {}) or {}

    def _roles_from_any(raw: Any) -> Optional[Dict[str, List[int]]]:
        if isinstance(raw, dict) and raw:
            return _dedupe_roles(_normalize_roles(raw))
        return None

    def _score(roles: Optional[Dict[str, List[int]]]) -> tuple:
        if not roles:
            return (0, 0, 0)
        return (len(roles.get("main", []) or []),
                len(roles.get("assist", []) or []),
                len(roles.get("admin", []) or []))

    def _build_slots_from_roles(roles: Dict[str, List[int]], trainee_val: Any = None) -> Dict[str, Any]:
        slots: Dict[str, Any] = {}
        for i, uid in enumerate(roles.get("main", []) or [], start=1):
            slots[f"lead{i}"] = f"uid:{uid}|{uid}"
        for i, uid in enumerate(roles.get("assist", []) or [], start=1):
            slots[f"assistant{i}"] = f"uid:{uid}|{uid}"
        if roles.get("admin"):
            slots["admin"] = f"uid:{roles['admin'][0]}|{roles['admin'][0]}"
        if trainee_val is not None:
            slots["trainee"] = trainee_val
        return slots

    dc_cur = dc_all.get(did_s)
    roles_dc = _roles_from_any(dc_cur)

    pd = details_all.get(did) or {}
    pd_dist = pd.get("distribution") if isinstance(pd, dict) else None
    trainee_pd = pd_dist.get("trainee") if isinstance(pd_dist, dict) else None
    roles_pd = _roles_from_any(pd_dist)

    legacy = legacy_all.get(did)
    trainee_legacy = legacy.get("trainee") if isinstance(legacy, dict) else None
    roles_legacy = _roles_from_any(legacy)

    src_roles: Optional[Dict[str, List[int]]] = None
    if roles_pd and (_score(roles_pd) > _score(roles_dc) or (roles_dc and roles_pd != roles_dc)):
        src_roles = roles_pd
    elif roles_dc:
        src_roles = roles_dc
    elif roles_legacy:
        src_roles = roles_legacy

    if not src_roles:
        return None

    if roles_pd and src_roles is roles_pd:
        slots = _build_slots_from_roles(src_roles, trainee_val=trainee_pd)
    elif roles_legacy and src_roles is roles_legacy:
        slots = _build_slots_from_roles(src_roles, trainee_val=trainee_legacy)
    else:
        slots = dict(dc_cur) if isinstance(dc_cur, dict) else _build_slots_from_roles(src_roles)

    slots = _dedupe_slots(slots)

    dc_all[did_s] = dict(slots)
    details_all.setdefault(did, {})["distribution"] = dict(slots)
    legacy_all[did] = dict(slots)

    return _dedupe_roles(_normalize_roles(slots))


def _extract_distribution_from_cache(deal_id: int) -> Optional[Dict[str, List[int]]]:
    """
    ЕДИНЫЙ источник — state.distribution_cache[str(id)].
    Перед чтением всегда выполняем мягкую миграцию/сверку с зеркалами.
    Возвращаем роли (dedupe) ИСКЛЮЧИТЕЛЬНО на основе содержимого единого кэша после reconcile.
    """
    _ensure_state_structs()
    did = int(deal_id)
    did_s = str(did)

    _migrate_from_mirrors_to_single_cache(did)
    dc_all = getattr(state, "distribution_cache", {}) or {}
    dc = dc_all.get(did_s)

    if isinstance(dc, dict) and dc:
        return _dedupe_roles(_normalize_roles(dc))
    return None


def _need_admin_for_deal(deal_id: int) -> bool:
    """
    Требуется ли администратор по пакету сделки.
    """
    _ensure_state_structs()
    deals = getattr(state, "current_poll_deals", []) or []
    d = next((x for x in deals if int(x.get("id") or 0) == int(deal_id)), None)
    if not d:
        return False
    pkg = str(d.get("package") or "").strip().lower()
    return pkg in {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}


def _role_cfg_local(game_name: str) -> Dict[str, int]:
    """
    Безопасно получает конфиг ролей по названию игры.
    Пытаемся взять из handlers.polls_lifecycle._role_cfg, иначе даём минимальный фолбэк.
    """
    try:
        import handlers.polls_lifecycle as _plc  # type: ignore
        cfg = _plc._role_cfg(game_name)  # type: ignore[attr-defined]
        main_need = int(cfg.get("main_leaders", cfg.get("main", 1)))
        asst_need = int(cfg.get("assistants", cfg.get("assist", 1)))
        return {"main_leaders": max(main_need, 0), "assistants": max(asst_need, 0)}
    except Exception:
        return {"main_leaders": 1, "assistants": 1}


def _resolve_svetofor_func():
    """
    Находит функцию get_user_status_from_svetофор (async/sync). Возвращает call-able.
    Приоритет: handlers.poll_details → services.gsheets. Если недоступно — безопасный фолбэк.
    """
    try:
        from handlers.poll_details import get_user_status_from_svetofor  # type: ignore
        return get_user_status_from_svetofor
    except Exception:
        pass
    try:
        from services.gsheets import get_user_status_from_svetofor  # type: ignore
        return get_user_status_from_svetofor
    except Exception:
        async def _fallback(_uid: int, _game: str) -> str:
            return "yellow"
        return _fallback


async def _derive_team_roles(deal_id: int) -> Dict[str, List[int]]:
    """
    Компонуем состав из ответов + Светофора, если кэши пусты.
    Инварианты/приоритеты:
      • 1 uid → 1 роль (used-сет),
      • RED/пустые статусы не попадают в core-ролях (main/assist),
      • порядок выбора: Светофор (green < yellow) → месячный счётчик (меньше → выше) → uid.
    """
    _ensure_state_structs()

    # найдём сделку и конфиг потребностей
    deals = getattr(state, "current_poll_deals", []) or []
    deal = next((d for d in deals if int(d.get("id") or 0) == int(deal_id)), None)
    if not deal:
        return {"main": [], "assist": [], "admin": []}

    game_name = str(deal.get("game_name") or deal.get("name") or "")
    cfg = _role_cfg_local(game_name)
    need_main, need_assist = int(cfg["main_leaders"]), int(cfg["assistants"])

    team: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}
    used: Set[int] = set()

    # светофор и месячные счётчики
    svetofor_fn = _resolve_svetofor_func()
    monthly = await _get_monthly_counters()

    # соберём пул откликнувшихся по этой сделке
    responses: Dict[str, Any] = getattr(state, "responses", {}) or {}
    raw_pool: Set[int] = set()
    for pdata in responses.values():
        deals_map: Dict[Any, Any] = pdata.get("deals") or {}
        users_raw = deals_map.get(deal_id, deals_map.get(str(deal_id), [])) or []
        for u in users_raw:
            try:
                uid = int(u.get("user_id") or 0)
            except Exception:
                uid = 0
            if uid:
                raw_pool.add(uid)

    # статусы светофора по пулу
    async def _sv(uid: int) -> Tuple[int, str]:
        st = svetofor_fn(uid, game_name)
        if asyncio.iscoroutine(st):
            st = await st
        return uid, str(st or "").lower()

    sv_map: Dict[int, str] = {}
    if raw_pool:
        pairs = await asyncio.gather(*[_sv(u) for u in raw_pool], return_exceptions=True)
        for p in pairs:
            if isinstance(p, Exception):
                continue
            uid, st = p
            sv_map[int(uid)] = st

    def _sv_rank(s: str) -> int:
        s = (s or "").lower()
        if s == "green":
            return 0
        if s == "yellow":
            return 1
        if s == "red":
            return 2
        return 3

        # отсортированные кандидаты по приоритету
    candidates = sorted(
        list(raw_pool),
        key=lambda uid: (_sv_rank(sv_map.get(uid, "")), int(monthly.get(uid, 0)), int(uid)),
    )

    # ПРАВИЛЬНЫЕ пулы по «Светофору»:
    #   • main  — только GREEN
    #   • assist — GREEN и YELLOW
    pool_main = [u for u in candidates if _sv_rank(sv_map.get(u, "")) == 0]
    pool_ass  = [u for u in candidates if _sv_rank(sv_map.get(u, "")) in (0, 1)]

    # набираем main
    for uid in pool_main:
        if len(team["main"]) >= need_main:
            break
        if uid in used:
            continue
        team["main"].append(uid)
        used.add(uid)

    # набираем assist
    for uid in pool_ass:
        if len(team["assist"]) >= need_assist:
            break
        if uid in used:
            continue
        team["assist"].append(uid)
        used.add(uid)


    # админ — сначала из единого кэша (если вручную уже выбран), иначе из ответов "admin_available" с приоритетами
    dc_all = getattr(state, "distribution_cache", {}) or {}
    cached = dc_all.get(str(deal_id)) or {}
    if isinstance(cached, dict) and cached.get("admin"):
        team["admin"] = _as_user_list(cached.get("admin"))

    if not team["admin"]:
        assigned = set(team["main"]) | set(team["assist"])
        admin_ids: Set[int] = set()
        for pdata in responses.values():
            for adm in (pdata.get("admin_available") or []):
                try:
                    uid_a = int(adm.get("user_id") or 0)
                except Exception:
                    uid_a = 0
                if uid_a and uid_a not in assigned:
                    admin_ids.add(uid_a)
        if admin_ids:
            admin_sorted = sorted(list(admin_ids), key=lambda uid: (_sv_rank(sv_map.get(uid, "")), int(monthly.get(uid, 0)), int(uid)))
            if admin_sorted:
                team["admin"] = [admin_sorted[0]]

    return _dedupe_roles(team)


async def _get_current_team(deal_id: int, invoker_uid: Optional[int] = None) -> Dict[str, List[int]]:
    """
    Актуальный состав ролей по сделке.

    Правила:
    • Единый источник чтения — state.distribution_cache[str(deal_id)] (перед этим выполняется reconcile).
    • Если пусто — derive из ответов (_derive_team_roles) и материализуем в distribution_cache
      в слотовом виде с плейсхолдерами (имена подставятся на этапе форматирования/коммита).
    • Инвариант «1 uid → 1 роль» соблюдается на каждом шаге.
    • Если пакет сделки требует администратора — добираем его по приоритету:
      Светофор (green < yellow < red/—) → месячный счётчик (меньше — выше) → uid.
    """
    # 1) прочитать из единого кэша (с мягкой миграцией из зеркал)
    roles = _extract_distribution_from_cache(int(deal_id))

    # 2) если пусто — derive и материализовать в кэш (слоты с плейсхолдерами)
    if not roles or not (roles.get("main") or roles.get("assist") or roles.get("admin")):
        roles = await _derive_team_roles(int(deal_id))
        roles = _dedupe_roles(roles or {"main": [], "assist": [], "admin": []})

        # материализация только если есть что записывать
        if any(roles.values()):
            try:
                slots = _slots_from_roles_placeholder(roles)
                did_s = str(int(deal_id))
                # единый кэш
                getattr(state, "distribution_cache")[did_s] = dict(slots)  # type: ignore[index]
                # зеркала (историческая совместимость)
                pd = getattr(state, "poll_details")
                pd.setdefault(int(deal_id), {})["distribution"] = dict(slots)
                getattr(state, "poll_distribution")[int(deal_id)] = dict(slots)  # type: ignore[index]
            except Exception:
                # материализация — best-effort, не критично
                pass

    # 3) финальная дедупликация
    roles = _dedupe_roles(roles or {"main": [], "assist": [], "admin": []})

    # 4) при необходимости — подобрать администратора
    if _need_admin_for_deal(int(deal_id)) and not roles.get("admin"):
        # 4.1 сначала попробуем взять из единого кэша (если его уже выбирали вручную)
        dc_all = getattr(state, "distribution_cache", {}) or {}
        raw = dc_all.get(str(int(deal_id)), {})
        if isinstance(raw, dict) and raw.get("admin"):
            roles["admin"] = _as_user_list(raw["admin"])

        # 4.2 если всё ещё нет — выбрать из «admin_available», исключая уже занятых
        if not roles.get("admin"):
            assigned: Set[int] = set(roles.get("main", [])) | set(roles.get("assist", []))
            responses: Dict[str, Any] = getattr(state, "responses", {}) or {}

            admin_ids: List[int] = []
            for pdata in responses.values():
                for adm in (pdata.get("admin_available") or []):
                    try:
                        uid = int(adm.get("user_id") or 0)
                    except Exception:
                        uid = 0
                    if uid and uid not in assigned:
                        admin_ids.append(uid)

            if admin_ids:
                # контекст для приоритизации
                # название игры — из текущих сделок (без падений при отсутствии)
                deals = getattr(state, "current_poll_deals", []) or []
                deal_rec = next((d for d in deals if int(d.get("id") or 0) == int(deal_id)), None)
                game_name = str((deal_rec or {}).get("game_name") or (deal_rec or {}).get("name") or "")

                # статусы светофора (поддержка sync/async)
                svetofor_fn = _resolve_svetofor_func()

                async def _sv(uid: int) -> Tuple[int, str]:
                    st = svetofor_fn(uid, game_name)
                    if asyncio.iscoroutine(st):
                        st = await st
                    return uid, str(st or "").lower()

                sv_map: Dict[int, str] = {}
                try:
                    pairs = await asyncio.gather(*[_sv(u) for u in admin_ids], return_exceptions=True)
                    for p in pairs:
                        if isinstance(p, Exception):
                            continue
                        uid, st = p
                        sv_map[int(uid)] = st
                except Exception:
                    sv_map = {}

                # месячные счётчики (AmoCRM) — безопасный фолбэк на {}
                monthly = await _get_monthly_counters()

                def _sv_rank(s: str) -> int:
                    s = (s or "").lower()
                    if s == "green":
                        return 0
                    if s == "yellow":
                        return 1
                    if s == "red":
                        return 2
                    return 3  # неизвестное/пусто — в конец

                admin_ids = sorted(
                    set(admin_ids),
                    key=lambda uid: (_sv_rank(sv_map.get(uid, "")), int(monthly.get(uid, 0)), int(uid)),
                )
                if admin_ids:
                    roles["admin"] = [admin_ids[0]]

    # 5) финальный возврат с гарантиями инвариантов
    return _dedupe_roles(roles or {"main": [], "assist": [], "admin": []})

# История изменений:
# • 2025-08-24 — добавлена приоритизация по месячным счётчикам (AmoCRM) в _derive_team_roles/_get_current_team;
#                инициализация state.monthly_role_counters в _ensure_state_structs; выровнено под SSOT.

# ════════════════════════════════════════════════════════════════════
# [1.t] Мини-тесты блока
# ════════════════════════════════════════════════════════════════════
def _test() -> None:
    _ensure_state_structs()

    # алиасы на SSOT: парсер/лист/нормализация
    assert _parse_uid("Иван П.|123") == 123
    assert _as_user_list(["1", 2, "3"]) == [1, 2, 3]
    norm = _normalize_roles({"lead1": "А А.|1", "assistant1": "B B.|2", "admin": "C C.|3"})
    assert set(norm.keys()) == {"main", "assist", "admin"}

    # инвариант I1 (1 человек = 1 роль), приоритет main > assist > admin
    roles = {"main": [1, 2], "assist": [2, 3], "admin": [3, 4]}
    ded = _dedupe_roles(roles)
    assert ded == {"main": [1, 2], "assist": [3], "admin": [4]}

    # дедуп слотов: повторяющийся uid в менее приоритетных слотах должен очиститься
    slots = {"lead1": "Имя Ф.|10", "assistant1": "Имя Ф.|10", "admin": "Имя Ф.|10", "trainee": "Кто-то|20"}
    ds = _dedupe_slots(slots)
    assert ds["lead1"] and ds["assistant1"] == "" and ds["admin"] == ""

# История изменений:
# • 2025-08-20 — выровнено под SSOT, удалены локальные дубли (_parse_uid/_as_user_list/_normalize_roles),
#                добавлен адаптер «Светофора» без ошибок Pylance; инварианты и публичные имена сохранены.

# ════════════════════════════════════════════════════════════════════
# [1.1] КОАЛЕСЦИРОВАННЫЙ РЕДРАВ «МОИ ИГРЫ» — ОТКЛЮЧЕНО
# ════════════════════════════════════════════════════════════════════
def _queue_redraw_my_games(uids: Set[int], delay_sec: float = 0.15) -> None:
    """
    Раньше планировался один редрав «Мои игры» для пачки uid.
    Теперь — выключено, чтобы не уводить пользователя из отчёта после «Утвердить».
    Ссылку на личный кабинет даём только в уведомлении (inline-кнопка).
    """
    return

# История изменений: [1.1] 2025-08-20 — редрав отключён; вызовы оставлены как NOP.


# ────────────────────────────────────────────────────────────────────
# [1.x] ИМЕНА: короткое «Имя Ф.» и форматирование списков уведомления
# ────────────────────────────────────────────────────────────────────
from typing import Any, Dict, List
from contextlib import suppress
import re

# SSOT: используем единые хелперы, но сохраняем прежние имена-обёртки
from core.utils import (
    short_name as _ssot_short_name,
    team_bulleted_lines as _ssot_team_bulleted_lines,
)

async def _short_name(uid: int) -> str:
    """
    Обёртка над SSOT core.utils.short_name(uid) → строго «Имя Ф.»
    (между именем и инициалом пробел, после инициала точка).
    """
    raw = await _ssot_short_name(uid)
    s = " ".join(str(raw or "").strip().split())

    # Уже в нужном виде «Имя Ф.»
    parts = s.split()
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].endswith("."):
        return s

    # «Имя Ф» (без точки) → добавим точку
    if len(parts) == 2 and len(parts[1]) == 1:
        return f"{parts[0]} {parts[1].upper()}."

    # «Имя Фамилия [Отчество]» → «Имя Ф.»
    if len(parts) >= 2:
        first = parts[0]
        last_initial = parts[-1][:1].upper() if parts[-1] else ""
        if first and last_initial:
            return f"{first} {last_initial}."

    # Попробуем достать фамилию из индекса пользователей в state
    with suppress(Exception):
        from core.state import state as _state  # локальный импорт во избежание циклов
        urec = (_state.users_index or {}).get(int(uid), {})  # type: ignore[attr-defined]
        fn = str(urec.get("first_name") or urec.get("fname") or "").strip()
        ln = str(urec.get("last_name") or urec.get("lname") or urec.get("surname") or "").strip()
        if fn and ln:
            return f"{fn.split()[0]} {ln[:1].upper()}."
        if fn:
            return fn.split()[0]

    # Фолбэк: оставляем как есть (лучше «Имя», чем пусто)
    return parts[0] if parts else "User"

async def _fmt(uid_: int, role_key: str) -> str:
    """
    Формирует «Имя Ф.<суффикс>|uid» для записи в слоты.
    Суффиксы: main→.1, assist→.2, admin→.Адм, trainee→.Стаж
    """
    name = await _short_name(uid_)
    suffix = {
        "main": ".1",
        "assist": ".2",
        "admin": ".Адм",
        "trainee": ".Стаж",
    }.get(role_key, "")
    val = (name + suffix).replace("..1", ".1").replace("..2", ".2").replace("..Адм", ".Адм").replace("..Стаж", ".Стаж")
    return f"{val}|{uid_}".strip()

async def _team_bulleted_lines(roles: Dict[str, List[int]], deal_id: int) -> List[str]:
    """
    Обёртка: строим «слоты» из ролей и отдаём их в SSOT team_bulleted_lines(slots).
    Ставит суффиксы .1/.2/.Адм/.Стаж по slot-ключам (lead*/assistant*/admin/trainee).
    """
    slots: Dict[str, Any] = {}

    # main → lead{i}
    for i, uid in enumerate(roles.get("main", []) or [], start=1):
        nm = await _short_name(uid)  # строго «Имя Ф.»
        slots[f"lead{i}"] = f"{nm}|{uid}"

    # assist → assistant{i}
    for i, uid in enumerate(roles.get("assist", []) or [], start=1):
        nm = await _short_name(uid)
        slots[f"assistant{i}"] = f"{nm}|{uid}"

    # admin (первый, если есть)
    if roles.get("admin"):
        uid = roles["admin"][0]
        nm = await _short_name(uid)
        slots["admin"] = f"{nm}|{uid}"

    # trainee подтягиваем из единого кэша (как и прежде)
    with suppress(Exception):
        from core.state import state as _state  # локальный импорт, чтобы избежать циклов на стадии линтинга
        raw = (_state.distribution_cache or {}).get(str(deal_id), {})
        if isinstance(raw, dict) and raw.get("trainee"):
            slots["trainee"] = raw.get("trainee")

    # единый формат бульлетов — только через SSOT
    lines: List[str] = await _ssot_team_bulleted_lines(slots)
    return lines or ["• —"]

# История изменений:
# 2025-08-20 — выровнено под SSOT; добавлена строгая нормализация до формата «Имя Ф.».



# ────────────────────────────────────────────────────────────────────
# [1.k] КНОПКИ/УВЕДОМЛЕНИЯ/КОММИТ СОСТАВА (без редравов «Мои игры»)
# ────────────────────────────────────────────────────────────────────
from typing import Any, Dict, List, Optional, Set
from contextlib import suppress
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

async def _approval_announce_kb() -> "InlineKeyboardMarkup":
    """Кнопка в уведомлении: deep-link в «🎲 Мои игры» (тексты без изменений)."""
    from aiogram import Bot
    bot = Bot.get_current()
    uname = ""
    with suppress(Exception):
        me = await bot.get_me()
        uname = (me.username or "").strip()
    if not uname:
        from core.config import settings as _settings
        from core.state import state as _state
        uname = str(getattr(_settings, "BOT_USERNAME", "") or getattr(_state, "bot_username", "") or "bot")
    url = f"https://t.me/{uname}?start=my_games"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Личный кабинет", url=url)]])

def _mark_approved_on_message_kb(callback: "CallbackQuery", deal_id: int) -> Optional["InlineKeyboardMarkup"]:
    """Перекрашивает «Утвердить» → «✅ Утверждено» только в текущем сообщении."""
    try:
        msg = getattr(callback, "message", None)
        if not msg or not isinstance(msg.reply_markup, InlineKeyboardMarkup):
            return None
        new_rows: List[List[InlineKeyboardButton]] = []
        for row in msg.reply_markup.inline_keyboard or []:
            new_row: List[InlineKeyboardButton] = []
            for btn in row or []:
                cdata = getattr(btn, "callback_data", "") or ""
                if cdata == f"poll_approve_{deal_id}":
                    new_row.append(InlineKeyboardButton(text="✅ Утверждено", callback_data="noop"))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_rows)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception("[approve] failed to rebuild keyboard")
        return None

def _slot_uid_from_label(val: Any) -> Optional[int]:
    """Принимает 'Имя Ф.|123' / int / None → возвращает uid или None."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1].strip()
        return int(s) if s.isdigit() else None
    return None

async def _commit_locked_distribution_to_state(deal_id: int, roles: Dict[str, List[int]]) -> Dict[str, Any]:
    """
    Фиксируем утверждённый состав:
      locked_distribution[deal_id]                      ← «Имя Ф.<суффикс>|uid»
      pending_confirmations[deal_id]['distribution']    ← то же
      distribution_cache[str(deal_id)] / poll_details   ← то же
      assigned_index[uid]                               ← deal_id
    ВАЖНО: никаких редравов/переходов в «Мои игры».
    """
    from core.state import state as _state
    from contextlib import suppress as _suppress

    slots: Dict[str, Any] = {}
    for i, uid in enumerate(roles.get("main", []) or [], start=1):
        slots[f"lead{i}"] = await _fmt(uid, "main")
    for i, uid in enumerate(roles.get("assist", []) or [], start=1):
        slots[f"assistant{i}"] = await _fmt(uid, "assist")
    if roles.get("admin"):
        slots["admin"] = await _fmt(roles["admin"][0], "admin")

    with _suppress(Exception):
        raw = (_state.distribution_cache or {}).get(str(deal_id), {})
        if isinstance(raw, dict) and raw.get("trainee"):
            t_val = raw.get("trainee")
            t_uid = _slot_uid_from_label(t_val)
            slots["trainee"] = await _fmt(int(t_uid), "trainee") if t_uid else str(t_val)

    # инвариант «1 пользователь = 1 роль»
    slots = _dedupe_slots(slots)

    # запись во все точки правды
    _state.locked_distribution[deal_id] = dict(slots)
    _state.pending_confirmations[deal_id] = {"distribution": dict(slots), "confirmed": set()}
    _state.distribution_cache[str(deal_id)] = dict(slots)
    pd = _state.poll_details.setdefault(deal_id, {})
    pd["distribution"] = dict(slots)

    # индекс для быстрых выборок «Мои игры» (без редравов)
    all_uids: Set[int] = set()
    for v in slots.values():
        u = _slot_uid_from_label(v)
        if u:
            all_uids.add(u)
    for uid in all_uids:
        idx = _state.assigned_index.setdefault(uid, set())
        idx.add(deal_id)

    return slots

# История изменений: [1.k] 2025-08-20 — убраны любые вызовы redraw_my_games / create_task; остальная логика без изменений.


# ════════════════════════════════════════════════════════════════════
# [1] INLINE-КЛАВИАТУРА (резерв под действия)
# ════════════════════════════════════════════════════════════════════
def distribution_actions_markup() -> InlineKeyboardMarkup:
    """Нижняя action-панель для отчёта лидеру (зарезервировано под будущее)."""
    return InlineKeyboardMarkup(inline_keyboard=[])

# История изменений: [1-inline] добавлен 2025-08-13, без логики (резерв)


# ════════════════════════════════════════════════════════════════════
# [2] УТВЕРЖДЕНИЕ СОСТАВА / LOCKED DISTRIBUTION — РЕЗЕРВ (без дубликатов)
# ════════════════════════════════════════════════════════════════════
"""
В этом блоке раньше повторно объявлялась `distribution_actions_markup()`,
из-за чего происходило перекрытие реализации из секции [1] и появлялись
варнинги линтера/IDE.

ФИКС:
— Убираем любые дубли и обработчики из этого блока.
— Не объявляем функций/хендлеров, чтобы сохранить единственный источник
  правды для действий: реализация `distribution_actions_markup()` остаётся в [1],
  логика утверждения/назначения и уведомлений — в блоках [3], [4], [5].
— Блок оставлен намеренно как «резерв» для нумерации и будущих расширений.
"""

# намеренно пусто — всё, что связано с клавиатурами/утверждением,
# находится в секциях [1], [1.k], [3], [4] и [5].

# ════════════════════════════════════════════════════════════════════
# [3] HANDLER: Утвердить одну игру (без автопереходов) — FIX header+admin+dedupe (+cache)
# ════════════════════════════════════════════════════════════════════
import inspect
from contextlib import suppress
from typing import Any, Optional, Dict, List

@router.callback_query(lambda c: c.data and c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    """
    Утверждение одной игры. Без автопереходов:
    • перекраска кнопки «Утвердить» → «✅ Утверждено» в текущем сообщении,
    • запись состава (включая admin, если он есть в кэше/деталях) в locked_distribution,
    • чат-уведомление с deep-link в «🎲 Личный кабинет».
    """

    # ── локальный импорт plc, чтобы Pylance не ругался на неопределённый идентификатор
    plc: Any = None
    with suppress(Exception):
        import handlers.polls_lifecycle as _plc  # type: ignore
        plc = _plc

    # ── локальный хелпер: короткая шапка для уведомления «Название. ДД.ММ ЧЧ:ММ. Пакет. N чел.»
    def _build_header_sentence(did: int) -> str:
        from datetime import datetime, time as _time

        def _as_date_str(val: Any) -> str:
            try:
                if isinstance(val, datetime):
                    return val.strftime("%d.%m.%Y")
                s = str(val or "").strip()
                if len(s) == 10 and s[2] in ".-":
                    return s.replace("-", ".")
                return s
            except Exception:
                return str(val)

        def _as_time_str(val: Any) -> str:
            try:
                if isinstance(val, datetime):
                    return val.strftime("%H:%M")
                if isinstance(val, _time):
                    return val.strftime("%H:%M")
                s = str(val or "").strip()
                # 930/0930 → 09:30
                if s.isdigit() and len(s) in (3, 4):
                    s = s.rjust(4, "0")
                    return f"{s[:2]}:{s[2:]}"
                s = s.replace(".", ":").replace("-", ":")
                return s[:5] if len(s) >= 5 and len(s) > 2 and s[2] == ":" else s
            except Exception:
                return str(val)

        deals = getattr(state, "current_poll_deals", []) or []
        d = next((x for x in deals if int(x.get("id") or 0) == int(did)), None)
        meta = (getattr(state, "deals_index", {}) or {}).get(int(did), {})  # type: ignore[index]

        title = str((d or {}).get("game_name") or (d or {}).get("name") or meta.get("title") or f"Сделка #{did}").strip()

        if d and d.get("event_datetime"):
            try:
                date_s = _as_date_str(d["event_datetime"])
                time_dt = _as_time_str(d["event_datetime"])
            except Exception:
                date_s = _as_date_str(d.get("event_date") or meta.get("date"))
                time_dt = ""
        else:
            date_s = _as_date_str((d or {}).get("event_date") or meta.get("date"))
            time_dt = ""

        # приоритет кастомного времени (из CF)
        time_s = _as_time_str((d or {}).get("event_time")) or _as_time_str(meta.get("time")) or time_dt
        cf = (d or {}).get("custom_fields") or (d or {}).get("cf") or {}
        if isinstance(cf, dict):
            time_s = (
                time_s
                or _as_time_str(cf.get("event_time"))
                or _as_time_str(cf.get("time"))
                or _as_time_str(cf.get("custom_time"))
                or _as_time_str(cf.get("amocrm_event_time"))
            )

        pkg = str((d or {}).get("package") or meta.get("package") or "").strip()
        players_raw = (d or {}).get("players") or (d or {}).get("players_count") or meta.get("players")
        players = ""
        try:
            players_i = int(players_raw)
            players = f"{players_i} чел."
        except Exception:
            players = str(players_raw or "").strip()
            players = f"{players} чел." if players else ""

        parts = [title, f"{date_s} {time_s}".strip(), pkg, players]
        out = ". ".join(p for p in parts if p).strip()
        return (out.rstrip(".") + ".")

    # ── разбор callback
    try:
        deal_id = int(str(callback.data).rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Ошибка: неизвестный формат callback.", show_alert=True)
        return

    # уже утверждено → просто перекрасить кнопку и ACK
    if int(deal_id) in (getattr(state, "locked_distribution", {}) or {}):
        with suppress(Exception):
            kb = _mark_approved_on_message_kb(callback, int(deal_id))
            if kb and callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
        await callback.answer("Уже утверждено ✅")
        return

    # проверка готовности по текущему distribution_cache (учитывает требуемого админа)
    if plc and hasattr(plc, "_is_deal_ready"):
        is_ready = await plc._is_deal_ready(int(deal_id))  # type: ignore[attr-defined]
        if not is_ready:
            await callback.answer("Минимальный состав ещё не набран.", show_alert=True)
            return

    # базовые роли (mains/assists) + admin, если уже есть в кэше/деталях
    roles = await _get_current_team(int(deal_id), callback.from_user.id)

    # ────────────────────────────────────────────────────────────────
    # CACHE FIX: если основа пустая/усечённая, восстановим роли из зеркал:
    # 1) poll_details[deal_id]['distribution'] (формат lead*/assistant*/admin = "Имя|uid")
    # 2) distribution_cache[str(deal_id)] / distribution_cache[int(deal_id)]
    # 3) locked_distribution[int(deal_id)] (если повторное утверждение сорвалось ранее)
    # ────────────────────────────────────────────────────────────────
    def _roles_from_slots(slots: Dict[str, Any]) -> Dict[str, List[int]]:
        main: List[int] = []
        assist: List[int] = []
        admin: List[int] = []
        if not isinstance(slots, dict):
            return {"main": main, "assist": assist, "admin": admin}
        # lead*
        for k in sorted([k for k in slots if isinstance(k, str) and k.startswith("lead")]):
            uid = _parse_uid(slots.get(k))
            if uid:
                main.append(uid)
        # assistant*
        for k in sorted([k for k in slots if isinstance(k, str) and k.startswith("assistant")]):
            uid = _parse_uid(slots.get(k))
            if uid:
                assist.append(uid)
        # admin (строка "Имя|uid" или числовой uid)
        adm_uid = _parse_uid(slots.get("admin"))
        if adm_uid:
            admin = [adm_uid]
        return {"main": main, "assist": assist, "admin": admin}

    def _merge_if_empty(base: Dict[str, List[int]], extra: Dict[str, List[int]]) -> Dict[str, List[int]]:
        # заполняем только те роли, которые пустые
        out = {k: list(base.get(k, [])) for k in ("main", "assist", "admin")}
        for k in ("main", "assist", "admin"):
            if not out[k] and extra.get(k):
                out[k] = list(extra[k])
        return out

    # если пустые или отсутствуют main/assist — пробуем зеркала
    need_fill = not (roles.get("main") or roles.get("assist"))
    if need_fill:
        # 1) poll_details
        with suppress(Exception):
            pd = (getattr(state, "poll_details", {}) or {}).get(int(deal_id), {}) or {}
            extra = _roles_from_slots(pd.get("distribution") or {})
            roles = _merge_if_empty(roles, extra)

        # 2) distribution_cache (оба типа ключей)
        with suppress(Exception):
            dc = (getattr(state, "distribution_cache", {}) or {})
            raw = dc.get(str(int(deal_id))) or dc.get(int(deal_id)) or {}
            extra = _roles_from_slots(raw if isinstance(raw, dict) else {})
            roles = _merge_if_empty(roles, extra)

        # 3) locked_distribution
        with suppress(Exception):
            ld = (getattr(state, "locked_distribution", {}) or {}).get(int(deal_id)) or {}
            extra = _roles_from_slots(ld if isinstance(ld, dict) else {})
            roles = _merge_if_empty(roles, extra)
    # ────────────────────────────────────────────────────────────────
    # /CACHE FIX
    # ────────────────────────────────────────────────────────────────

    # ДЕДУП 1: гарантия «1 uid → 1 роль» для main/assist уже сейчас
    if callable(globals().get("_dedupe_roles")):
        roles = _dedupe_roles(roles)  # type: ignore[misc]

    # ── ВАЖНО: протаскиваем администратора, если он выбран вручную (даже если пакет не обязует)
    if not (roles.get("admin") and len(roles["admin"]) > 0):
        admin_uid: Optional[int] = None
        # 1) детали (если ранее открывали/синхронизировали)
        with suppress(Exception):
            pd = (getattr(state, "poll_details", {}) or {}).get(int(deal_id)) or {}
            dist_pd = pd.get("distribution") or {}
            if isinstance(dist_pd, dict) and isinstance(dist_pd.get("admin"), str):
                admin_uid = _parse_uid(dist_pd.get("admin"))
        # 2) distribution_cache
        if not admin_uid:
            with suppress(Exception):
                raw = (getattr(state, "distribution_cache", {}) or {}).get(str(int(deal_id))) or {}
                if isinstance(raw, dict) and isinstance(raw.get("admin"), str):
                    admin_uid = _parse_uid(raw.get("admin"))  # type: ignore[arg-type]
        if admin_uid:
            roles["admin"] = [admin_uid]

    # ДЕДУП 2 (КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ): повторно устраняем дубли,
    # если admin совпал с кем-то из main/assist. Приоритет main > assist > admin.
    if callable(globals().get("_dedupe_roles")):
        roles = _dedupe_roles(roles)  # type: ignore[misc]

    # роли пустые → нечего коммитить
    if not _uids_from_roles(roles):
        logger.warning("[approve] deal %s has empty roles after normalization", deal_id)
        await callback.answer("Нет текущего распределения. Откройте детали и расставьте роли.", show_alert=True)
        return

    # фиксируем слоты в состоянии (locked_distribution + зеркала)
    slots: Dict[str, Any] = {}
    try:
        res = _commit_locked_distribution_to_state(int(deal_id), roles)
        if inspect.isawaitable(res):  # поддержка старой async-сигнатуры
            res = await res  # type: ignore[assignment]
        if isinstance(res, dict):
            slots = res  # новая сигнатура могла вернуть слоты
        if not slots:
            # берём из state как «источник правды»
            slots = ((getattr(state, "locked_distribution", {}) or {}).get(int(deal_id)) or {})  # type: ignore[assignment]
    except Exception as e:
        logger.error("[approve] commit failed: %s", e)
        await callback.answer("Не удалось зафиксировать состав, попробуйте ещё раз.", show_alert=True)
        return

    # перекрашиваем кнопку «Утвердить» только в этом сообщении (без перерисовки дашборда)
    with suppress(Exception):
        kb = _mark_approved_on_message_kb(callback, int(deal_id))
        if kb and callback.message:
            await callback.message.edit_reply_markup(reply_markup=kb)

    with suppress(Exception):
        await callback.answer("Игра утверждена ✅")

    # уведомление в рабочий чат (с админом, если он есть)
    try:
        bot = callback.message.bot if callback.message else None
        if bot:
            chat_id = await _resolve_notify_chat_id(bot)
            if chat_id is not None:
                head = _build_header_sentence(int(deal_id))
                lines = await _team_bulleted_lines(roles, int(deal_id))  # включает «🛡️ Администратор», если он есть
                text = (
                    f"✅ Состав команды на игру {head} утверждён.\n"
                    + "\n".join(lines)
                    + "\nПодтвердите участие в личном кабинете!"
                )
                kb2 = await _approval_announce_kb()
                await bot.send_message(chat_id, text, reply_markup=kb2)
            else:
                logger.error("[approve] no available chat for notify; skipped")
    except Exception as e:
        logger.error("[approve] notify chat failed: %s", e)

    # ────────────────────────────────────────────────────────────────
    # МЯГКИЙ АПДЕЙТ ОТЧЁТА ЛИДЕРУ (БЕЗ «ПЫЛЕСОСА» И ПЕРЕРИСОВКИ ДАШБОРДА)
    # ────────────────────────────────────────────────────────────────
    with suppress(Exception):
        from aiogram import Bot  # локальный импорт для Pylance
        bot2 = callback.message.bot if callback.message else Bot.get_current()

        leader_id = getattr(state, "current_poll_leader", None)
        msg_id = getattr(state, "personal_report_message_id", None)
        if leader_id and msg_id and plc and hasattr(plc, "_build_report_keyboard"):
            kb_report = plc._build_report_keyboard()  # type: ignore[attr-defined]
            await bot2.edit_message_reply_markup(chat_id=leader_id, message_id=msg_id, reply_markup=kb_report)
            logger.debug("[approve] soft report kb update: leader=%s msg=%s", leader_id, msg_id)

    logger.info("[approve] deal %s approved by %s; slots=%s", deal_id, callback.from_user.id, slots)

# История изменений:
#  • 2025-08-15 — FIX: восстановления ролей из зеркал при пустом distribution_cache (string/int keys), без изменения остальной логики.
#  • 2025-08-12 — header+admin+dedupe улучшения.



# ════════════════════════════════════════════════════════════════════
# [4] HANDLER: Утвердить все готовые (батч, без автопереходов) — FIX header
# ════════════════════════════════════════════════════════════════════
from typing import List, Tuple, Optional  # локальные типы

@router.callback_query(lambda c: c.data == "approve_all_ready")
async def poll_approve_all_ready_handler(callback: CallbackQuery) -> None:
    """
    Утверждает все игры, где набран минимальный состав.
    Без автопереходов/редравов «Моих игр». На каждую игру — корректное чат-уведомление.
    """

    # локальный хелпер вместо внешнего _deal_header_sentence
    def _build_header_sentence(did: int) -> str:
        from datetime import datetime, time as _time

        def _as_date_str(val) -> str:
            if not val:
                return ""
            try:
                if isinstance(val, datetime):
                    return val.strftime("%d.%m.%Y")
                s = str(val).strip()
                if len(s) == 10 and s[2] in ".-":
                    return s.replace("-", ".")
                return s
            except Exception:
                return str(val)

        def _as_time_str(val) -> str:
            if not val:
                return ""
            try:
                if isinstance(val, datetime):
                    return val.strftime("%H:%M")
                if isinstance(val, _time):
                    return val.strftime("%H:%M")
                s = str(val).strip()
                if s.isdigit() and len(s) in (3, 4):
                    s = s.rjust(4, "0")
                    return f"{s[:2]}:{s[2:]}"
                s = s.replace(".", ":").replace("-", ":")
                if len(s) >= 5 and s[2] == ":":
                    return s[:5]
                return s
            except Exception:
                return str(val)

        d = next((x for x in (getattr(state, "current_poll_deals", None) or [])
                  if int(x.get("id") or 0) == int(did)), None)
        meta = (getattr(state, "deals_index", {}) or {}).get(did, {})

        title = str((d or {}).get("game_name") or (d or {}).get("name") or meta.get("title") or f"Сделка #{did}").strip()

        if d and d.get("event_datetime"):
            try:
                date_s = _as_date_str(d["event_datetime"])
                time_dt = _as_time_str(d["event_datetime"])
            except Exception:
                date_s = _as_date_str(d.get("event_date") or meta.get("date"))
                time_dt = ""
        else:
            date_s = _as_date_str((d or {}).get("event_date") or meta.get("date"))
            time_dt = ""

        time_s = (
            _as_time_str((d or {}).get("event_time"))
            or _as_time_str(meta.get("time"))
            or time_dt
        )

        cf = (d or {}).get("custom_fields") or (d or {}).get("cf") or {}
        if isinstance(cf, dict):
            time_s = (
                time_s
                or _as_time_str(cf.get("event_time"))
                or _as_time_str(cf.get("time"))
                or _as_time_str(cf.get("custom_time"))
                or _as_time_str(cf.get("amocrm_event_time"))
            )

        pkg = str((d or {}).get("package") or meta.get("package") or "").strip()
        players = str((d or {}).get("players") or (d or {}).get("players_count") or meta.get("players") or "").strip()

        parts = [title, f"{date_s} {time_s}".strip(), pkg, (f"{players} чел." if players else "")]
        out = ". ".join(p for p in parts if p).strip()
        return (out.rstrip(".") + ".")

    try:
        await callback.answer("Обрабатываю…")
    except Exception:
        pass

    approved: List[int] = []
    skipped: List[Tuple[int, str]] = []

    bot = callback.message.bot if callback.message else None
    chat_id: Optional[int] = None
    if bot:
        chat_id = await _resolve_notify_chat_id(bot)

    for deal in list(state.current_poll_deals or []):
        did = int(deal.get("id") or 0)
        if not did:
            continue
        try:
            if not await _is_deal_ready(did):
                skipped.append((did, "not_ready"))
                continue
            if did in (getattr(state, "locked_distribution", {}) or {}):
                approved.append(did)
                continue

            roles = await _get_current_team(did, callback.from_user.id)
            if not _uids_from_roles(roles):
                skipped.append((did, "no_roles"))
                continue

            await _commit_locked_distribution_to_state(did, roles)
            approved.append(did)

            # чат-уведомление (с локальным заголовком)
            if bot and chat_id is not None:
                try:
                    head = _build_header_sentence(did)
                    lines = await _team_bulleted_lines(roles, did)
                    text = (
                        f"✅ Состав команды на игру {head} утверждён.\n"
                        + "\n".join(lines)
                        + "\nПодтвердите участие в личном кабинете."
                    )
                    kb2 = await _approval_announce_kb()
                    await bot.send_message(chat_id, text, reply_markup=kb2)
                except Exception as e:
                    logger.error("[approve_all] notify chat failed for %s: %s", did, e)

        except Exception as e:
            logger.warning("[approve_all] deal %s failed: %s", did, e)
            skipped.append((did, "exception"))

    try:
        if approved:
            await callback.answer(f"Утверждено игр: {len(approved)} ✅")
        else:
            await callback.answer("Нет готовых игр для утверждения.", show_alert=True)
    except Exception:
        pass

    # ── МЯГКОЕ ОБНОВЛЕНИЕ ДАШБОРДА ОТЧЁТА ЛИДЕРУ (без «пылесоса» и без пересоздания сообщения)
    try:
        bot2 = callback.message.bot if callback.message else None
        leader_id = getattr(state, "current_poll_leader", None)
        msg_id = getattr(state, "personal_report_message_id", None)
        if bot2 and leader_id and msg_id:
            import handlers.polls_lifecycle as plc  # lazy импорт, чтобы не плодить зависимости
            kb_report = plc._build_report_keyboard()
            await bot2.edit_message_reply_markup(
                chat_id=leader_id,
                message_id=msg_id,
                reply_markup=kb_report
            )
    except Exception as e:
        logger.debug("[approve_all] soft report kb update failed: %s", e)

    # (!) НИКАКИХ полных редравов/«пылесоса» здесь больше не вызываем.
    # await _try_sync_report()  # заменено на мягкое обновление выше


# ════════════════════════════════════════════════════════════════════
# [5] ПРОЧИЕ HANDLERS: stop / back
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith("poll_stop_"))
async def poll_stop_game_handler(callback: CallbackQuery) -> None:
    try:
        deal_id = int((callback.data or "").rsplit("_", 1)[-1])
    except Exception:
        try:
            await callback.answer()
        except Exception:
            pass
        return
    if not getattr(state, "deal_force_closed", None):
        state.deal_force_closed = set()
    state.deal_force_closed.add(deal_id)
    try:
        await callback.answer("Набор остановлен.")
    except Exception:
        pass
    logger.info("[details] deal %d force-stopped by %d", deal_id, callback.from_user.id)

@router.callback_query(lambda c: c.data and c.data.startswith("poll_back_"))
async def poll_back_handler(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass
    await _try_sync_report()

# История изменений: [5] обновлён 2025-08-13 — back вызывает _try_sync_report()

# ════════════════════════════════════════════════════════════════════
# [99] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    """Быстрые проверки нормализации и форматирования (без внешних сервисов)."""
    raw = {"main": [1, "Иван И.|101"], "assist": ["202"], "admin": ["bad", 303, "303", "Петр|404", ["505", ["Сергей|606"]]]}
    norm = _normalize_roles(raw)
    assert norm == {"main": [1, 101], "assist": [202], "admin": [303, 303, 404, 505, 606]}
    dd = _dedupe_roles(norm)
    assert dd == {"main": [1, 101], "assist": [202], "admin": [303, 404, 505, 606]}
    assert _uids_from_roles(dd) == {1, 101, 202, 303, 404, 505, 606}
    print("handlers.polls_distribution ✅ tests passed")

if __name__ == "__main__":
    import asyncio as _a, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    _a.run(_test())

# История изменений: [99] добавлен 2025‑08‑13 — smoke‑тест нормализации