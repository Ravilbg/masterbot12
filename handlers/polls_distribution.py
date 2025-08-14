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

from __future__ import annotations

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
from handlers.my_games import redraw_my_games
import handlers.polls_lifecycle as plc  # локальный импорт, чтобы избежать циклов
from services.gsheets import get_user_status_from_svetofor
import re  
logger = logging.getLogger(__name__)
router = Router(name="polls_distribution")

# алиас на проверку готовности сделки (из lifecycle)
_is_deal_ready = plc._is_deal_ready


async def _try_sync_report() -> None:
    """
    Совместимый вызов перерисовки отчёта после «Утвердить».
    Если в текущей версии lifecycle нет sync_report — тихо пропускаем.
    """
    try:
        fn = getattr(plc, "sync_report", None)
        if callable(fn):
            await fn()
    except Exception as e:
        logger.warning("[polls_dist] sync_report skipped: %s", e)

# История изменений: [0] обновлён 2025-08-13 — Router(name=…), InlineKeyboardButton, _try_sync_report()

# ════════════════════════════════════════════════════════════════════
# [1] УТИЛИТЫ: нормализация, commit в state, формат уведомлений
# ════════════════════════════════════════════════════════════════════
"""
Назначение:
• нормализовать состав (UID) из distribution_cache / poll_details;
• инварианта «1 uid → 1 роль» (main > assist > admin);
• собрать «слоты» под «Мои игры»: lead1/assistant1/admin/trainee «Имя Ф.<суффикс>|uid»;
• записать утверждённый состав в locked_distribution + poll_details.distribution + distribution_cache;
• аккуратно собрать заголовок и список участников для уведомления;
• локально перекрасить кнопку «Утвердить» → «✅ Утверждено».
"""

from typing import Any, Dict, List, Set, Tuple, Optional

def _ensure_state_structs() -> None:
    if not getattr(state, "assigned_index", None):
        state.assigned_index = {}            # dict[int, set[int]]
    if not getattr(state, "locked_distribution", None):
        state.locked_distribution = {}       # dict[int, dict[str,str]]
    if not getattr(state, "pending_confirmations", None):
        state.pending_confirmations = {}     # dict[int, dict]
    if not getattr(state, "distribution_cache", None):
        state.distribution_cache = {}        # dict[str, dict[str,str]]
    if not getattr(state, "poll_details", None):
        state.poll_details = {}              # dict[int, dict]
    if not getattr(state, "poll_distribution", None):
        state.poll_distribution = {}         # dict[int, dict]

def _parse_uid(val: Any) -> Optional[int]:
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        try:
            return int(s)
        except ValueError:
            return None
    return None

def _as_user_list(v: Any) -> List[int]:
    out: List[int] = []
    if v is None:
        return out
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        u = _parse_uid(v)
        return [u] if u is not None else out
    if isinstance(v, (list, tuple, set)):
        for x in v:
            out.extend(_as_user_list(x))
    return out

def _normalize_roles(raw: Dict[str, Any]) -> Dict[str, List[int]]:
    """
    Приводит структуру к {'main':[uid...], 'assist':[uid...], 'admin':[uid...]}.
    Поддерживаются две схемы:
      1) Ролевая: main/main_leaders, assist/assistants, admin
      2) Слотовая: lead1/lead2..., assistant1/assistant2..., admin, trainee
    """
    if not isinstance(raw, dict):
        return {"main": [], "assist": [], "admin": []}

    # ── схема 1: ролевая
    has_role_keys = any(k in raw for k in ("main", "main_leaders", "assist", "assistants", "admin"))
    if has_role_keys:
        main_val = raw.get("main", raw.get("main_leaders"))
        assist_val = raw.get("assist", raw.get("assistants"))
        admin_val = raw.get("admin")
        return {
            "main": _as_user_list(main_val),
            "assist": _as_user_list(assist_val),
            "admin": _as_user_list(admin_val),
        }

    # ── схема 2: слотовая (ручное редактирование/детали)
    lead_keys = sorted([k for k in raw if isinstance(k, str) and k.startswith("lead")],
                       key=lambda k: int(re.search(r"(\d+)$", k).group(1)) if re.search(r"(\d+)$", k) else 0)
    asst_keys = sorted([k for k in raw if isinstance(k, str) and k.startswith("assistant")],
                       key=lambda k: int(re.search(r"(\d+)$", k).group(1)) if re.search(r"(\d+)$", k) else 0)

    mains = []
    for k in lead_keys:
        mains.extend(_as_user_list(raw.get(k)))

    assists = []
    for k in asst_keys:
        assists.extend(_as_user_list(raw.get(k)))

    admins = _as_user_list(raw.get("admin"))

    return {"main": mains, "assist": assists, "admin": admins}

def _dedupe_roles(roles: Dict[str, List[int]]) -> Dict[str, List[int]]:
    seen: Set[int] = set()
    out: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}
    for u in roles.get("main", []):
        if u and u not in seen:
            out["main"].append(u); seen.add(u)
    for u in roles.get("assist", []):
        if u and u not in seen:
            out["assist"].append(u); seen.add(u)
    for u in roles.get("admin", []):
        if u and u not in seen:
            out["admin"].append(u); seen.add(u)
    return out

def _uids_from_roles(roles: Dict[str, List[int]]) -> Set[int]:
    return set(roles.get("main", [])) | set(roles.get("assist", [])) | set(roles.get("admin", []))

def _extract_distribution_from_cache(deal_id: int) -> Optional[Dict[str, List[int]]]:
    """
    Возвращает роли из любого доступного кэша. Понимает как ролевую, так и слотовую схему.
    Приоритет: distribution_cache[str(id)] → poll_details[deal_id]['distribution'] → poll_distribution[deal_id]
    """
    dc = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id))
    if isinstance(dc, dict):
        return _normalize_roles(dc)

    details = (getattr(state, "poll_details", {}) or {}).get(deal_id) or {}
    if isinstance(details.get("distribution"), dict):
        return _normalize_roles(details["distribution"])

    dist3 = (getattr(state, "poll_distribution", {}) or {}).get(deal_id)
    if isinstance(dist3, dict):
        return _normalize_roles(dist3)

    return None

def _need_admin_for_deal(deal_id: int) -> bool:
    d = next((x for x in (state.current_poll_deals or []) if int(x.get("id") or 0) == int(deal_id)), None)
    if not d:
        return False
    pkg = str(d.get("package") or "").strip().lower()
    return pkg in {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}

async def _derive_team_roles(deal_id: int) -> Dict[str, List[int]]:
    """Компонуем из ответов + Светофора, если кэши пусты."""
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id") or 0) == deal_id), None)
    if not deal:
        return {"main": [], "assist": [], "admin": []}

    game_name = deal.get("game_name") or deal.get("name") or ""
    cfg = plc._role_cfg(game_name)  # type: ignore[attr-defined]
    need_main, need_assist = int(cfg["main_leaders"]), int(cfg["assistants"])

    team: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}
    used: Set[int] = set()

    for pdata in (state.responses or {}).values():
        users = (pdata.get("deals") or {}).get(deal_id, [])
        if not users:
            continue
        for u in users:
            uid = int(u.get("user_id") or 0)
            if not uid or uid in used:
                continue
            status = get_user_status_from_svetofor(uid, game_name)
            if asyncio.iscoroutine(status):
                status = await status
            if status == "green":
                if len(team["main"]) < need_main:
                    team["main"].append(uid); used.add(uid); continue
                if len(team["assist"]) < need_assist:
                    team["assist"].append(uid); used.add(uid); continue
            elif status == "yellow":
                if len(team["assist"]) < need_assist:
                    team["assist"].append(uid); used.add(uid); continue

    # админ — из кэша либо из ответов «админ доступен»
    cached = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id)) or {}
    if isinstance(cached, dict) and cached.get("admin"):
        team["admin"] = _as_user_list(cached.get("admin"))
    if not team["admin"]:
        assigned = set(team["main"]) | set(team["assist"])
        for pdata in (state.responses or {}).values():
            for adm in (pdata.get("admin_available") or []):
                uid = int(adm.get("user_id") or 0)
                if uid and uid not in assigned:
                    team["admin"] = [uid]
                    break
            if team["admin"]:
                break

    return team

async def _get_current_team(deal_id: int, invoker_uid: Optional[int] = None) -> Dict[str, List[int]]:
    """
    Возвращает актуальный состав ролей, обязательно доукомплектуя админа,
    если пакет требует и он отсутствует в кэшах.
    """
    roles = _extract_distribution_from_cache(deal_id)
    if roles is None or (not roles.get("main") and not roles.get("assist")):
        roles = await _derive_team_roles(deal_id)
    roles = _dedupe_roles(roles or {"main": [], "assist": [], "admin": []})

    # Гарантия админа для требующих пакетов
    if _need_admin_for_deal(deal_id) and not roles.get("admin"):
        raw = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
        if isinstance(raw, dict) and raw.get("admin"):
            roles["admin"] = _as_user_list(raw["admin"])

        if not roles.get("admin"):
            assigned = set(roles.get("main", [])) | set(roles.get("assist", []))
            for pdata in (state.responses or {}).values():
                for adm in (pdata.get("admin_available") or []):
                    uid = int(adm.get("user_id") or 0)
                    if uid and uid not in assigned:
                        roles["admin"] = [uid]
                        break
                if roles.get("admin"):
                    break

    return _dedupe_roles(roles)

# ────────────────────────────────────────────────────────────────────
# [1.x] ИМЕНА: короткое «Имя Ф.» и форматирование списков уведомления
# ────────────────────────────────────────────────────────────────────
import asyncio                               # локально — чтобы не зависеть от верхних импортов
from contextlib import suppress              # локально — чтобы не требовать contextlib выше
from typing import Any, Dict, List           # локально — чтобы не зависеть от [0] IMPORTS

async def _short_name(uid: int) -> str:
    """
    Возвращает короткое имя в формате «Имя Ф.» (с точкой).
    Источники (по приоритету):
      1) core.db.get_user_info: first_name, last_name_initial | last_name
      2) state.user_short[uid] — уже готовое «Имя Ф.»
      3) state.users[uid]: first_name, last_name_initial | last_name
    Фолбэк — строковый uid.
    """
    ui: Dict[str, Any] = {}

    # 1) core.db.get_user_info (поддержка sync/async)
    with suppress(Exception):
        from core.db import get_user_info  # локальный импорт для совместимости
        res = get_user_info(uid)
        ui = await res if asyncio.iscoroutine(res) else (res or {})
        if not isinstance(ui, dict):
            ui = {}

    # 2) готовое «Имя Ф.» из state.user_short
    with suppress(Exception):
        short_ready = (getattr(state, "user_short", {}) or {}).get(uid)
        if isinstance(short_ready, str) and short_ready.strip():
            return short_ready.strip()

    # 3) доп. источник: state.users[uid]
    if not ui:
        with suppress(Exception):
            ui = ((getattr(state, "users", {}) or {}).get(uid) or {})  # type: ignore[assignment]

    first = str(ui.get("first_name") or ui.get("name") or "").strip()
    last_initial_raw = str(ui.get("last_name_initial") or "").strip()
    last_full = str(ui.get("last_name") or ui.get("surname") or "").strip()

    def _norm_initial(li: str, last: str) -> str:
        """Нормализует инициал до формата «К.» (или пусто)."""
        base = (li or "").replace(".", "").strip() or (last[:1] if last else "")
        return f"{base[0].upper()}." if base else ""

    li = _norm_initial(last_initial_raw, last_full)

    if not (first or li):
        return str(uid)

    return f"{first} {li}".strip()

async def _fmt(uid_: int, role_key: str) -> str:
    """
    Возвращает строку для слотов/тегов: «Имя Ф.суффикс|uid».
    Суффиксы: main→.1, assist→.2, admin→.Адм, trainee→.Стаж
    """
    name = await _short_name(uid_)
    suffix = { "main": ".1", "assist": ".2", "admin": ".Адм", "trainee": ".Стаж" }.get(role_key, "")
    # устраняем возможные двойные точки
    val = (name + suffix).replace("..1", ".1").replace("..2", ".2").replace("..Адм", ".Адм").replace("..Стаж", ".Стаж")
    return f"{val}|{uid_}".strip()

async def _team_bulleted_lines(roles: Dict[str, List[int]], deal_id: int) -> List[str]:
    """
    Формирует пункты для уведомления:
      • Имя Ф.1
      • Имя Ф.2
      • Имя Ф.Адм
      • Имя Ф.Стаж (если trainee указан в кэше)
    """
    lines: List[str] = []

    for uid in roles.get("main", []) or []:
        nm = await _short_name(uid)
        lines.append(f"• {nm}.1".replace("..1", ".1"))

    for uid in roles.get("assist", []) or []:
        nm = await _short_name(uid)
        lines.append(f"• {nm}.2".replace("..2", ".2"))

    for uid in roles.get("admin", []) or []:
        nm = await _short_name(uid)
        lines.append(f"• {nm}.Адм".replace("..Адм", ".Адм"))

    # стажёр берётся из distribution_cache[str(deal_id)]
    with suppress(Exception):
        raw = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
        if isinstance(raw, dict) and raw.get("trainee"):
            # используем глобальный _parse_uid, если он есть; иначе пытаемся распарсить локально
            t_val = raw.get("trainee")
            t_uid = None
            try:
                # предпочитаем глобальную функцию, если она объявлена в модуле
                t_uid = globals().get("_parse_uid", lambda v: int(str(v).rsplit("|", 1)[-1]))(t_val)  # type: ignore
            except Exception:
                with suppress(Exception):
                    t_uid = int(str(t_val).rsplit("|", 1)[-1])
            if t_uid:
                nm = await _short_name(int(t_uid))
                lines.append(f"• {nm}.Стаж".replace("..Стаж", ".Стаж"))

    return lines or ["• —"]

# История изменений: 2025-08-14 — блок переписан: локальные импорты asyncio/suppress/typing,
# стабильная нормализация «Имя Ф.», безопасный парс стажёра без жалоб Pylance.


# ────────────────────────────────────────────────────────────────────
# [1.k] КНОПКИ/УВЕДОМЛЕНИЯ/КОММИТ СОСТАВА (самодостаточный блок)
# ────────────────────────────────────────────────────────────────────
from typing import Any, Dict, List, Optional, Set  # локальные импорты типов
from contextlib import suppress

async def _approval_announce_kb() -> "InlineKeyboardMarkup":
    """Кнопка в уведомлении: deep-link в «🎲 Мои игры»."""
    # локальные импорты, чтобы не зависеть от верхних
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    bot = Bot.get_current()
    with suppress(Exception):
        me = await bot.get_me()
        uname = (me.username or "").strip()
    if not uname:
        # фолбэк на настройки/состояние
        uname = str(getattr(settings, "BOT_USERNAME", "") or getattr(state, "bot_username", "") or "bot")
    url = f"https://t.me/{uname}?start=my_games"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Личный кабинет", url=url)]])

def _mark_approved_on_message_kb(callback: "CallbackQuery", deal_id: int) -> Optional["InlineKeyboardMarkup"]:
    """В текущем сообщении перекрашивает кнопку «Утвердить» → «✅ Утверждено»."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    try:
        msg = getattr(callback, "message", None)
        if not msg:
            return None
        kb = getattr(msg, "reply_markup", None)
        if not isinstance(kb, InlineKeyboardMarkup):
            return None

        new_rows: List[List[InlineKeyboardButton]] = []
        for row in kb.inline_keyboard or []:
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
        logger.exception("[approve] failed to rebuild keyboard")
        return None

async def _resolve_notify_chat_id(bot: Any) -> Optional[int]:
    """
    Возвращает первый доступный чат для уведомлений:
    POLLS_CHAT_ID → LEADERS_CHAT_ID → state.admin_chat_id → ADMIN_CHAT_ID.
    Валидируем доступ через get_chat (мягко).
    """
    candidates = [
        getattr(settings, "POLLS_CHAT_ID", None),
        getattr(settings, "LEADERS_CHAT_ID", None),
        getattr(state, "admin_chat_id", None),
        getattr(settings, "ADMIN_CHAT_ID", None),
    ]
    for cid in candidates:
        if not cid:
            continue
        with suppress(Exception):
            cid_int = int(str(cid).strip())
            # проверяем, что бот видит чат
            await bot.get_chat(cid_int)
            return cid_int
        logger.warning("[polls_dist] notify chat %s not accessible", cid)
    return None

async def _commit_locked_distribution_to_state(deal_id: int, roles: Dict[str, List[int]]) -> Dict[str, str]:
    """
    Синхронизируем точки правды (ФОРМАТ совместим с «Мои игры»):
      locked_distribution[deal_id]  ← {'lead1': 'Имя Ф.1|uid', 'assistant1': 'Имя Ф.2|uid',
                                       'admin': 'Имя Ф.Адм|uid', 'trainee': 'Имя Ф.Стаж|uid'?}
      pending_confirmations[deal_id]['distribution'] ← тот же dict
      distribution_cache[str(deal_id)] ← тот же dict
      poll_details[deal_id]['distribution'] ← тот же dict
      assigned_index[uid] ← deal_id
    """
    _ensure_state_structs()

    # безопасный вызов внешнего билдера, если он объявлен выше; иначе — локальный фолбэк
    to_slot = globals().get("_to_slot_distribution")
    if callable(to_slot):
        slots: Dict[str, str] = await to_slot(deal_id, roles)  # type: ignore[misc]
    else:
        # ── локальный фолбэк-билдер слотов ─────────────────────────────
        slots = {}
        # основные
        for i, uid in enumerate(roles.get("main", []) or [], start=1):
            slots[f"lead{i}"] = await _fmt(uid, "main")
        # ассистенты
        for i, uid in enumerate(roles.get("assist", []) or [], start=1):
            slots[f"assistant{i}"] = await _fmt(uid, "assist")
        # админ (первый)
        if roles.get("admin"):
            slots["admin"] = await _fmt(roles["admin"][0], "admin")
        # стажёр — из кэша распределения
        with suppress(Exception):
            raw = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
            if isinstance(raw, dict) and raw.get("trainee"):
                t_val = raw.get("trainee")
                t_uid = None
                # пробуем использовать глобальный парсер, затем простой «|uid»
                with suppress(Exception):
                    _p = globals().get("_parse_uid")
                    if callable(_p):
                        t_uid = _p(t_val)  # type: ignore[call-arg]
                if t_uid is None:
                    with suppress(Exception):
                        t_uid = int(str(t_val).rsplit("|", 1)[-1])
                slots["trainee"] = await _fmt(int(t_uid), "trainee") if t_uid else str(t_val)
        # ───────────────────────────────────────────────────────────────

    # запись в кэши
    state.locked_distribution[deal_id] = dict(slots)
    state.pending_confirmations[deal_id] = {"distribution": dict(slots), "confirmed": set()}

    state.distribution_cache[str(deal_id)] = dict(slots)
    pd = state.poll_details.setdefault(deal_id, {})
    pd["distribution"] = dict(slots)

    # индекс назначений для «Мои игры»
    all_uids: Set[int] = set()

    def _parse_uid_safe(v: Any) -> Optional[int]:
        with suppress(Exception):
            fn = globals().get("_parse_uid")
            if callable(fn):
                return int(fn(v))  # type: ignore[call-arg]
        with suppress(Exception):
            return int(str(v).rsplit("|", 1)[-1])
        return None

    for v in slots.values():
        u = _parse_uid_safe(v)
        if u:
            all_uids.add(u)

    for uid in all_uids:
        idx = state.assigned_index.setdefault(uid, set())
        idx.add(deal_id)

    logger.debug("[polls_dist] deal %d locked+committed; slots=%s", deal_id, slots)
    return slots

# История изменений: 2025-08-14 — блок переписан:
# • локальные импорты и suppress → без жалоб Pylance;
# • безопасный lookup _to_slot_distribution + фолбэк-билдер;
# • устойчивые deep-link и перекраска inline-клавиатуры;
# • безопасный парс uid из слотов.


# ════════════════════════════════════════════════════════════════════
# [1] INLINE-КЛАВИАТУРА (резерв под действия)
# ════════════════════════════════════════════════════════════════════
def distribution_actions_markup() -> InlineKeyboardMarkup:
    """Нижняя action-панель для отчёта лидеру (зарезервировано под будущее)."""
    return InlineKeyboardMarkup(inline_keyboard=[])

# История изменений: [1-inline] добавлен 2025-08-13, без логики (резерв)


# ════════════════════════════════════════════════════════════════════
# [1.1] Коалесцирование перерисовок «Мои игры» (фикс гонок «пылесоса»)
# ════════════════════════════════════════════════════════════════════
def _queue_redraw_my_games(uids: Set[int], delay_sec: float = 0.15) -> None:
    """
    Складываем uid в аккумулятор и планируем ОДНУ задачу,
    которая через короткую паузу перерисует дашборд для каждого uid ровно один раз.
    """
    if not uids:
        return

    acc: Set[int] = getattr(state, "_redraw_accum", set())
    acc |= set(uids)
    setattr(state, "_redraw_accum", acc)

    task: asyncio.Task | None = getattr(state, "_redraw_task", None)
    if task and not task.done():
        return

    async def _runner():
        try:
            await asyncio.sleep(delay_sec)
            batch: Set[int] = getattr(state, "_redraw_accum", set())
            setattr(state, "_redraw_accum", set())
            for uid in sorted(batch):
                try:
                    await redraw_my_games(uid)
                except Exception as e:
                    logger.error("[redraw_coalesce] uid=%s failed: %s", uid, e)
        finally:
            setattr(state, "_redraw_task", None)

    setattr(state, "_redraw_task", asyncio.create_task(_runner()))

# История изменений: [1.1] добавлен 2025-08-13 — коалесцированный редрав


# ════════════════════════════════════════════════════════════════════
# [2] INLINE-КЛАВИАТУРА (резерв под действия)
# ════════════════════════════════════════════════════════════════════
"""
Здесь раньше повторно определялась distribution_actions_markup(), из-за чего
происходило перекрытие функции и появлялись предупреждения линтера/IDE.

Исправление:
— Дубликат удалён. Используем единственную реализацию из секции [1].
— Блок оставлен как «резерв» без кода, чтобы сохранить нумерацию и стиль.
"""

# намеренно пусто — актуальная функция distribution_actions_markup() объявлена в [1]

# История изменений: 2025-08-12 — удалён дублирующийся def distribution_actions_markup()


# ════════════════════════════════════════════════════════════════════
# [3] HANDLER: Утвердить одну игру (без автопереходов) — FIX header+admin
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    """
    Утверждение одной игры. Без автопереходов:
    • перекраска кнопки «Утвердить» → «✅ Утверждено» в текущем сообщении,
    • запись состава (включая admin, если он есть в кэше/деталях) в locked_distribution,
    • чат-уведомление с deep-link в «🎲 Личный кабинет».
    """

    # ── локальный хелпер: короткая шапка для уведомления «Название. ДД.ММ ГГ:ММ. Пакет. N чел.»
    def _build_header_sentence(did: int) -> str:
        from datetime import datetime, time as _time

        def _as_date_str(val) -> str:
            try:
                if isinstance(val, datetime):
                    return val.strftime("%d.%m.%Y")
                s = str(val or "").strip()
                if len(s) == 10 and s[2] in ".-":
                    return s.replace("-", ".")
                return s
            except Exception:
                return str(val)

        def _as_time_str(val) -> str:
            try:
                if isinstance(val, datetime):
                    return val.strftime("%H:%M")
                if isinstance(val, _time):
                    return val.strftime("%H:%M")
                s = str(val or "").strip()
                if s.isdigit() and len(s) in (3, 4):
                    s = s.rjust(4, "0")
                    return f"{s[:2]}:{s[2:]}"
                s = s.replace(".", ":").replace("-", ":")
                return s[:5] if len(s) >= 5 and s[2] == ":" else s
            except Exception:
                return str(val)

        d = next((x for x in (getattr(state, "current_poll_deals", None) or [])
                  if int(x.get("id") or 0) == did), None)
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

        # приоритет кастомного времени
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
        players = str((d or {}).get("players") or (d or {}).get("players_count") or meta.get("players") or "").strip()

        parts = [title, f"{date_s} {time_s}".strip(), pkg, (f"{players} чел." if players else "")]
        out = ". ".join(p for p in parts if p).strip()
        return (out.rstrip(".") + ".")

    # ── разбор callback
    try:
        deal_id = int((callback.data or "").rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Ошибка: неизвестный формат callback.", show_alert=True)
        return

    # уже утверждено → просто перекрасить кнопку и ACK
    if deal_id in (getattr(state, "locked_distribution", {}) or {}):
        try:
            kb = _mark_approved_on_message_kb(callback, deal_id)
            if kb and callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await callback.answer("Уже утверждено ✅")
        return

    # проверка готовности по текущему distribution_cache (учитывает требуемого админа)
    if not await plc._is_deal_ready(deal_id):
        await callback.answer("Минимальный состав ещё не набран.", show_alert=True)
        return

    # базовые роли (mains/assists) + admin, если он уже есть в кэше для обяз. пакетов
    roles = await _get_current_team(deal_id, callback.from_user.id)

    # ── ВАЖНО: Протаскиваем администратора во всех случаях, даже если пакет не обязует.
    #           Ищем кандидата последовательно: poll_details.distribution → distribution_cache.
    if not (roles.get("admin") and len(roles["admin"]) > 0):
        admin_uid: Optional[int] = None

        # 1) детали (если ранее открывали/синхронизировали)
        try:
            pd = (getattr(state, "poll_details", {}) or {}).get(deal_id) or {}
            dist_pd = pd.get("distribution") or {}
            if isinstance(dist_pd, dict):
                adm_tag = dist_pd.get("admin")
                if isinstance(adm_tag, str):
                    admin_uid = _parse_uid(adm_tag)
        except Exception:
            admin_uid = admin_uid or None

        # 2) distribution_cache (автораспределение из ответов)
        if not admin_uid:
            try:
                raw = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id)) or {}
                adm_tag = raw.get("admin")
                if isinstance(adm_tag, str):
                    admin_uid = _parse_uid(adm_tag)
            except Exception:
                admin_uid = admin_uid or None

        if admin_uid:
            roles["admin"] = [admin_uid]

    # роли пустые → нечего коммитить
    if not _uids_from_roles(roles):
        logger.warning("[approve] deal %d has empty roles after normalization", deal_id)
        await callback.answer("Нет текущего распределения. Откройте детали и расставьте роли.", show_alert=True)
        return

    # фиксируем слоты в состоянии (locked_distribution + зеркала)
    slots = await _commit_locked_distribution_to_state(deal_id, roles)

    # перекрашиваем кнопку «Утвердить» только в этом сообщении (без перерисовки дашборда)
    try:
        kb = _mark_approved_on_message_kb(callback, deal_id)
        if kb and callback.message:
            await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception as e:
        logger.debug("[approve] edit_reply_markup failed: %s", e)

    with suppress(Exception):
        await callback.answer("Игра утверждена ✅")

    # уведомление в рабочий чат (с админом, если он есть)
    try:
        bot = callback.message.bot if callback.message else None
        if bot:
            chat_id = await _resolve_notify_chat_id(bot)
            if chat_id is not None:
                head = _build_header_sentence(deal_id)
                lines = await _team_bulleted_lines(roles, deal_id)  # включает «🛡️ Администратор», если он есть
                text = (
                    f"✅ Состав команды на игру {head} утверждён.\n"
                    + "\n".join(lines)
                    + "\nПодтвердите своё участие в личном кабинете!"
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
    try:
        # локальный импорт, чтобы не трогать верх файла и убрать предупреждение Pylance
        from aiogram import Bot  # noqa: WPS433
        bot2 = callback.message.bot if callback.message else Bot.get_current()

        leader_id = getattr(state, "current_poll_leader", None)
        msg_id = getattr(state, "personal_report_message_id", None)
        if leader_id and msg_id:
            kb_report = plc._build_report_keyboard()  # та же клавиатура, что использует отчёт
            await bot2.edit_message_reply_markup(chat_id=leader_id, message_id=msg_id, reply_markup=kb_report)
            logger.debug("[approve] soft report kb update: leader=%s msg=%s", leader_id, msg_id)
    except Exception as e:
        logger.debug("[approve] soft report kb update failed: %s", e)

    logger.info("[approve] deal %d approved by %d; slots=%s", deal_id, callback.from_user.id, slots)

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
