# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Подтверждения участия ведущими.

Версия v15.2 · 2025‑08‑13
──────────────────────────────────────────────────────────────────────────────
• «✅ Подтвердить» ставит персональный тег в AmoCRM (add‑only, без перезаписи).
• Надёжное определение роли и состава из locked_distribution (списки и слоты).
• Локальная отметка кнопки на «✅ Подтверждено» без переходов и «пылесоса».
• Уведомление в общий чат: «Имя Ф. подтвердил выход на игру „Название“ ДД.ММ ЧЧ:ММ ✅».
• Проверка полноты подтверждений по locked_distribution + pending_confirmations.
• Автоперевод в статус «Завершение сделки», если все требуемые роли подтвердили.
• Совместимость с sync/async core.db.get_user_info.
• Экспорт CONFIRM_PREFIX — для «🎲 Мои игры».
"""

from __future__ import annotations

# ███ [0] IMPORTS & CONSTANTS
# --------------------------------------------------------------------
import inspect
import logging
from contextlib import suppress
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from aiogram import Router, types
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from core.config import settings
from core.state import state

# AmoCRM (универсальные обёртки)
from services import amocrm as amo  # type: ignore

# Детали сделки — на случай принудительных перерисовок
try:
    from handlers.poll_details import refresh_deal_details  # type: ignore
except Exception:  # pragma: no cover
    refresh_deal_details = None  # type: ignore

# Профиль: get_user_info может быть sync или async — обработаем оба случая
try:
    from core.db import get_user_info  # type: ignore
except Exception:  # pragma: no cover
    get_user_info = None  # type: ignore

logger = logging.getLogger(__name__)
router = Router(name="confirmations")

# Префикс для inline‑кнопок подтверждения
CONFIRM_PREFIX = "confirm_role_"

# Доступные стадии «успеха» (любой из вариантов настроек)
OK_STATUS_ID = (
    getattr(settings, "FINISH_STAGE_ID", None)
    or getattr(settings, "SUCCESSFUL_STATUS_ID", None)
    or getattr(state, "OK_STATUS_ID", None)
)

# История изменений [0]: 2025‑08‑13 — единый Router name, безопасные импорты, OK_STATUS_ID fallback


# ███ [1] NAME/ROLE/TAG HELPERS
# --------------------------------------------------------------------
async def _short_name(uid: int) -> str:
    """
    Возвращает «Имя Ф.» (с точкой).
    • сначала core.db.get_user_info (если корутина — await; если sync — прямой вызов),
    • затем state.users,
    • иначе uid.
    """
    # 1) core.db.get_user_info (sync/async)
    if callable(get_user_info):
        try:
            if inspect.iscoroutinefunction(get_user_info):  # async версия
                ui = await get_user_info(uid)  # type: ignore
            else:  # sync версия
                ui = get_user_info(uid)  # type: ignore
        except Exception:
            ui = None
        if isinstance(ui, dict):
            first = (ui.get("first_name") or "").strip()
            last = (ui.get("last_name") or "").strip()
            last_ini = (ui.get("last_name_initial") or (last[:1].upper() + "." if last else "")).strip()
            base = f"{first} {last_ini}".strip()
            if base:
                return base

    # 2) fallback — state.users
    try:
        u = (getattr(state, "users", {}) or {}).get(uid) or {}
        first = (u.get("first_name") or "").strip()
        last_ini = (u.get("last_name_initial") or "").strip()
        base = f"{first} {last_ini}".strip()
        if base:
            return base
    except Exception:
        pass

    # 3) uid
    return str(uid)


def _role_suffix(role: str) -> str:
    """Суффикс для тега по роли (унифицировано с распределением)."""
    return {"main": ".1", "assist": ".2", "admin": ".Адм", "trainee": ".Стаж"}.get(role, "")


def _to_uid_list(v: Any) -> List[int]:
    """Преобразует значение в список uid: int | 'Имя|uid' | Iterable → [int]."""
    out: List[int] = []
    if v is None:
        return out
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        s = v.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        try:
            out.append(int(s))
        except ValueError:
            pass
        return out
    if isinstance(v, Iterable):
        for x in v:
            out.extend(_to_uid_list(x))
    return out


def _role_alias(key: str) -> str:
    """Нормализует ключ в одно из {'main','assist','admin','trainee'}."""
    k = (key or "").lower()
    if k.startswith("lead") or k == "main":
        return "main"
    if k.startswith("assist"):
        return "assist"
    if "admin" in k:
        return "admin"
    if "trainee" in k or "intern" in k or "стаж" in k:
        return "trainee"
    return k


def _assigned_uids_from_locked(deal_id: int) -> Dict[str, Set[int]]:
    """
    Читает state.locked_distribution в обоих форматах:
    • новый: {'main':[uids], 'assist':[uids], 'admin':[uids]}
    • слоты: {'lead1':'Имя|123', 'assistant1':'Имя|456', 'admin':'Имя|789'}
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
          or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
          or {}
    roles: Dict[str, Set[int]] = {"main": set(), "assist": set(), "admin": set(), "trainee": set()}

    # списковая схема
    for k in ("main", "assist", "admin", "trainee"):
        for v in _to_uid_list(raw.get(k)):
            roles[k].add(v)

    # слотная схема
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and (k.startswith(("lead", "assistant")) or "admin" in k or "trainee" in k):
                roles[_role_alias(k)].update(_to_uid_list(v))

    return roles


def _deal_when(deal_id: int) -> Tuple[str, str]:
    """
    Возвращает (date, time) строки для уведомлений.
    Источники: state.current_poll_deals → state.deals_index → пусто.
    """
    # 1) Текущие сделки опроса
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(deal_id):
                date_s = ""
                if d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                    date_s = d["event_datetime"].strftime("%d.%m.%Y")
                else:
                    date_s = str(d.get("event_date") or "")
                time_s = str(d.get("event_time") or "")
                return (date_s or "", time_s or "")
    # 2) Индекс сделок
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(deal_id) \
             or (getattr(state, "deals_index", {}) or {}).get(str(deal_id)) \
             or {}
        return (str(meta.get("date") or ""), str(meta.get("time") or ""))
    # 3) Пусто
    return ("", "")


def _deal_title_from_state(deal_id: int) -> str:
    """
    Возвращает короткий заголовок игры без обращения к UI/деталям.
    Источники: state.deal_titles → current_poll_deals → «Сделка #id».
    """
    with suppress(Exception):
        t = (getattr(state, "deal_titles", {}) or {}).get(deal_id) \
            or (getattr(state, "deal_titles", {}) or {}).get(str(deal_id))
        if t:
            return str(t)
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(deal_id):
                return str(d.get("game_name") or d.get("name") or f"Сделка #{deal_id}")
    return f"Сделка #{deal_id}"


def _mark_confirmed_on_message_kb(callback: CallbackQuery, deal_id: int, role: str) -> InlineKeyboardMarkup | None:
    """
    Локально меняем кнопку «Подтвердить» на «✅ Подтверждено» в текущем сообщении.
    Никаких открытий деталей/редравов.
    """
    try:
        kb = callback.message.reply_markup if callback.message else None
        if not isinstance(kb, InlineKeyboardMarkup):
            return None
        new_rows: List[List[InlineKeyboardButton]] = []
        target_cd = f"{CONFIRM_PREFIX}{deal_id}_{role}"
        for row in (kb.inline_keyboard or []):
            new_row: List[InlineKeyboardButton] = []
            for btn in row:
                cd = getattr(btn, "callback_data", "") or ""
                if cd == target_cd:
                    new_row.append(InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop"))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_rows)
    except Exception:
        logger.exception("[confirm] failed to rebuild keyboard")
    return None


# ███ [2] AMOCRM HELPERS (универсальные фолбэки)
# --------------------------------------------------------------------
_TEAM_SUFFIXES: Set[str] = {".1", ".2", ".Адм", ".Стаж"}


def _is_team_tag(name: str) -> bool:
    """Командный тег — любой, что оканчивается на один из служебных суффиксов."""
    return bool(name) and any(str(name).endswith(suf) for suf in _TEAM_SUFFIXES)


async def _amo_add_tag(lead_id: int, tag: str) -> bool:
    """
    Добавляет командный тег в сделку, НЕ затирая ранее поставленные.
    ВНИМАНИЕ: здесь ИСКЛЮЧИТЕЛЬНО add‑only сценарии, без массовой перезаписи списка.
    """
    try:
        # Предпочитаем точечную обёртку, если она есть в services.amocrm
        if hasattr(amo, "add_tag_to_lead"):
            ok = await amo.add_tag_to_lead(int(lead_id), tag)  # type: ignore[arg-type]
            return bool(ok)

        # Универсальный PATCH одной сделки: _embedded.tags, который у AmoCRM добавляет тег
        if hasattr(amo, "patch_lead"):
            ok = await amo.patch_lead(int(lead_id), {"_embedded": {"tags": [{"name": str(tag)}]}})  # type: ignore[arg-type]
            return bool(ok)

        logger.warning("[confirm] amo add-tag API not found; lead=%s tag=%s", lead_id, tag)
        return False
    except Exception as e:
        logger.error("[confirm] add tag failed lead=%s tag=%s: %s", lead_id, tag, e)
        return False


async def _amo_get_tags(lead_id: int) -> Set[str]:
    """
    Возвращает множество названий тегов сделки; устойчив к 204 и пустым данным.
    Если конкретной обёртки нет — возвращает пустое множество (не считается ошибкой).
    """
    try:
        if hasattr(amo, "get_deal_by_id"):
            d = await amo.get_deal_by_id(int(lead_id))  # type: ignore[arg-type]
            if isinstance(d, dict) and d.get("tags"):
                return {
                    str(t.get("name"))
                    for t in (d.get("tags") or [])
                    if isinstance(t, dict) and t.get("name")
                }
    except Exception:
        logger.debug("[confirm] get_deal_by_id failed for %s (tags not available)", lead_id)

    return set()


async def _amo_set_status_success(lead_id: int) -> bool:
    """Переводит сделку в «Завершение сделки» по ID стадии из настроек."""
    try:
        stage_id = getattr(settings, "FINISH_STAGE_ID", None) or getattr(settings, "SUCCESSFUL_STATUS_ID", None) or OK_STATUS_ID
        if stage_id is None:
            logger.warning("[confirm] SUCCESS status id not configured; lead=%s", lead_id)
            return False

        if hasattr(amo, "update_deal_status"):
            ok = await amo.update_deal_status(int(lead_id), str(stage_id))  # type: ignore[arg-type]
            return bool(ok)

        if hasattr(amo, "patch_lead"):
            ok = await amo.patch_lead(int(lead_id), {"status_id": int(stage_id)})  # type: ignore[arg-type]
            return bool(ok)

        logger.warning("[confirm] amo status API not found; lead=%s", lead_id)
        return False
    except Exception:
        logger.exception("[confirm] set status failed lead=%s", lead_id)
        return False


async def _resolve_notify_chat_id(bot) -> Optional[int]:
    """
    Возвращает первый доступный чат для уведомлений:
    POLLS_CHAT_ID → LEADERS_CHAT_ID → state.admin_chat_id → ADMIN_CHAT_ID.
    Валидирует доступ через get_chat (без падений).
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
        try:
            cid_int = int(str(cid).strip())
            await bot.get_chat(cid_int)
            return cid_int
        except Exception:
            logger.warning("[confirm] notify chat %s not accessible", cid)
    return None


# ███ [3] DETAILS & CONFIRMATION CHECK — CRM+LOCAL fallback
# --------------------------------------------------------------------
from typing import Dict, Set, List, Any, Optional, Iterable, Tuple
from contextlib import suppress
import logging

from core.state import state
from services.amocrm import get_deal_by_id  # CRM — источник истины по тегам

logger = logging.getLogger(__name__)

def _to_uid_list(v: Any) -> List[int]:
    """int | 'Имя|uid' | 'uid' | None | контейнеры → [uid, ...]."""
    out: List[int] = []
    if v is None:
        return out
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        s = v.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        with suppress(ValueError):
            out.append(int(s))
        return out
    if isinstance(v, (list, tuple, set)):
        for x in v:
            if isinstance(x, (list, tuple, set)):
                out.extend(_to_uid_list(x))
            else:
                out.extend(_to_uid_list(x))
    return out

def _role_alias(key: str) -> str:
    """assistant1 → assist; lead1/lead2 → main; admin → admin."""
    k = str(key).lower()
    if k.startswith("lead"):
        return "main"
    if k.startswith("assistant"):
        return "assist"
    return "admin" if "admin" in k else k

def _assigned_uids_from_locked(deal_id: int) -> Dict[str, Set[int]]:
    """
    Назначенные по зафиксированному распределению.
    Возвращает {'main': {…}, 'assist': {…}, 'admin': {…}}.
    """
    out: Dict[str, Set[int]] = {"main": set(), "assist": set(), "admin": set()}
    locked = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) or {}
    if not isinstance(locked, dict):
        return out
    for slot, val in locked.items():
        role = _role_alias(slot)
        out.setdefault(role, set()).update(_to_uid_list(val))
    return out

def _confirmed_from_state(deal_id: int) -> Dict[str, Set[int]]:
    """
    Локальные подтверждения (когда CRM недоступна). Формат:
    {'main': set[int], 'assist': set[int], 'admin': set[int]}.
    """
    out: Dict[str, Set[int]] = {"main": set(), "assist": set(), "admin": set()}
    pc = (getattr(state, "pending_confirmations", {}) or {}).get(deal_id) or {}
    conf = pc.get("confirmed")
    if isinstance(conf, dict):
        for k in ("main", "assist", "admin"):
            vals = conf.get(k)
            if isinstance(vals, set):
                out[k] |= {int(x) for x in vals}
            elif vals:
                out[k] |= set(_to_uid_list(vals))
    elif isinstance(conf, set):
        # старая плоская схема: раскидываем по назначенным ролям
        assigned = _assigned_uids_from_locked(deal_id)
        for k in ("main", "assist", "admin"):
            out[k] |= (assigned.get(k, set()) & conf)
    return out

async def _crm_confirmation_tags(deal_id: int) -> Set[str]:
    """
    Теги CRM по сделке, нормализованные в Set[str]. Если CRM отвечает 204
    или недоступна — возвращаем пустое множество (не бросаем исключений).
    """
    tags: Set[str] = set()
    try:
        deal = await get_deal_by_id(int(deal_id))
        raw = (deal or {}).get("_embedded", {}).get("tags", [])  # type: ignore[index]
        for t in raw or []:
            name = str(t.get("name") or "").strip()
            if name:
                tags.add(name)
    except Exception:
        logger.warning("[confirm] CRM tags unavailable for lead=%s (treating as empty)", deal_id)
    return tags

async def _expected_tag_map(deal_id: int) -> Dict[str, Set[str]]:
    """
    Для назначенных uid строим ожидаемые текстовые теги вида «Имя Ф.1/2/Адм».
    Возвращает {'main': {'Иван И.1', ...}, 'assist': {...}, 'admin': {...}}.
    """
    from handlers.confirmations import _short_name  # локальный импорт
    assigned = _assigned_uids_from_locked(deal_id)
    def _suffix(role: str) -> str:
        return "1" if role == "main" else ("2" if role == "assist" else "Адм")
    out: Dict[str, Set[str]] = {"main": set(), "assist": set(), "admin": set()}
    for role, uids in assigned.items():
        suf = _suffix(role)
        for u in uids:
            human = (getattr(state, "user_short", {}) or {}).get(u)
            if not human:
                # подстрахуемся на случай отсутствия кэша имён
                human = await _short_name(u)
            out[role].add(f"{human}.{suf}")
    return out

async def _all_required_confirmed(deal_id: int) -> bool:
    """
    True, если все назначенные роли подтвердили участие.
    Источник истины — теги CRM; если CRM недоступна/пуста, применяем
    консенсус-фолбэк по локальному state.pending_confirmations.
    """
    assigned = _assigned_uids_from_locked(deal_id)
    if not any(assigned.values()):
        return False

    # 1) пытаемся подтвердить по CRM-тегам
    expected = await _expected_tag_map(deal_id)
    crm_tags = await _crm_confirmation_tags(deal_id)
    if crm_tags:
        for role, exp in expected.items():
            # у админа ровно 1 слот, у остальных — по числу назначенных
            need = len(assigned.get(role, set()))
            have = len([x for x in exp if x in crm_tags])
            if have < max(need, 0):
                return False
        return True

    # 2) CRM пустая/недоступна → фолбэк по локальным подтверждениям
    local = _confirmed_from_state(deal_id)
    for role, uids in assigned.items():
        if not uids:
            continue
        if not (uids <= local.get(role, set())):
            return False
    return True

# История изменений: 2025-08-15 — добавлен CRM→LOCAL фолбэк, совместимость со старой схемой state.confirmed


# ███ [4] CALLBACK: CONFIRM ROLE — идемпотентность + единый источник правды
# --------------------------------------------------------------------
from typing import Optional, Tuple, List, Set, Dict
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

CONFIRM_PREFIX = "confirm_role_"  # экспортируется в другие модули

async def _mark_confirmed_on_message_kb(callback: CallbackQuery, deal_id: int, role: str) -> Optional[InlineKeyboardMarkup]:
    """
    Локально перекрашивает кнопку текущего сообщения на «✅ Подтверждено».
    Без редравов других сообщений (детали/дашборд подтянутся из state).
    """
    msg = getattr(callback, "message", None)
    kb = getattr(msg, "reply_markup", None)
    if not kb or not isinstance(kb, InlineKeyboardMarkup):
        return None

    new_rows: List[List[InlineKeyboardButton]] = []
    for row in (kb.inline_keyboard or []):
        new_row: List[InlineKeyboardButton] = []
        for btn in row:
            cd = getattr(btn, "callback_data", "") or ""
            if cd == f"{CONFIRM_PREFIX}{deal_id}_{role}":
                new_row.append(InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop"))
            else:
                new_row.append(btn)
        new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)

async def _amo_add_tag(lead_id: int, tag_text: str) -> bool:
    """
    Добавляет тег в AmoCRM (add-only). Возвращает True при видимом успехе.
    Лояльна к различным фасадам services.amocrm.
    """
    try:
        if hasattr(amo, "add_tag_to_lead"):
            ok = await amo.add_tag_to_lead(int(lead_id), tag_text)  # type: ignore[arg-type]
            return bool(ok)
        if hasattr(amo, "patch_lead"):
            # общий фолбэк — добавить в массив tags
            ok = await amo.patch_lead(int(lead_id), {"add_tags": [tag_text]})  # type: ignore[arg-type]
            return bool(ok)
    except Exception:
        logger.exception("[confirm] add tag failed lead=%s tag=%s", lead_id, tag_text)
    return False

async def _amo_set_status_success(lead_id: int) -> bool:
    """Переводит сделку в успешный статус (OK_STATUS_ID)."""
    try:
        stage_id = (OK_STATUS_ID or getattr(settings, "SUCCESSFUL_STATUS_ID", None))
        if not stage_id:
            logger.warning("[confirm] SUCCESS status id not configured; lead=%s", lead_id)
            return False
        if hasattr(amo, "update_deal_status"):
            return bool(await amo.update_deal_status(int(lead_id), str(stage_id)))  # type: ignore[arg-type]
        if hasattr(amo, "patch_lead"):
            return bool(await amo.patch_lead(int(lead_id), {"status_id": int(stage_id)}))  # type: ignore[arg-type]
    except Exception:
        logger.exception("[confirm] set status failed lead=%s", lead_id)
    return False

async def _role_of_user_in_locked(uid: int, deal_id: int) -> Optional[str]:
    """main/assist/admin/None — по зафиксированному распределению."""
    locked = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) or {}
    if not isinstance(locked, dict):
        return None
    for slot, val in locked.items():
        role = _role_alias(slot)
        if uid in set(_to_uid_list(val)):
            return role
    return None

async def _perform_confirm(callback: CallbackQuery, deal_id: int, role: str) -> None:
    """
    Пайплайн подтверждения:
    1) Валидация: пользователь действительно назначен на роль/сделку.
    2) Идемпотентность: если уже подтвержден (CRM или локально) — только UI-метка.
    3) Пытаемся проставить тег в CRM; независимо от CRM пишем локальный флаг.
    4) Меняем кнопку на «✅ Подтверждено», шлём одно уведомление в общий чат.
    5) Если все роли подтверждены (CRM→LOCAL), переводим в «Завершение сделки».
    """
    uid = callback.from_user.id
    user_role = await _role_of_user_in_locked(uid, deal_id)
    if user_role != role:
        await callback.answer("⛔ Вы не назначены на эту роль.", show_alert=True)
        return

    # Инициализация хранилищ состояния (совместимость со старыми сборками)
    state.__dict__.setdefault("pending_confirmations", {})
    state.__dict__.setdefault("confirmed", {})

    # Идемпотентность: если уже подтвержден — просто перекрашиваем кнопку
    expected = await _expected_tag_map(deal_id)
    crm_tags = await _crm_confirmation_tags(deal_id)
    local = _confirmed_from_state(deal_id)
    human_expected = expected.get(role, set())

    already = False
    if crm_tags and any(tag in crm_tags for tag in human_expected):
        already = True
    if uid in local.get(role, set()):
        already = True

    if already:
        kb = await _mark_confirmed_on_message_kb(callback, deal_id, role)
        with suppress(Exception):
            if kb:
                await callback.message.edit_reply_markup(reply_markup=kb)
        with suppress(Exception):
            await callback.answer("Уже подтверждено ✅")
        return

    # 1) CRM-тег (лучшее усилие) +  локальный флаг подтверждения
    # строим текст тега: «Имя Ф.» + .1/.2/.Адм
    from handlers.confirmations import _short_name  # локальный импорт
    human = (getattr(state, "user_short", {}) or {}).get(uid) or (await _short_name(uid))
    suffix = {"main": "1", "assist": "2", "admin": "Адм"}[role]
    tag_text = f"{human}.{suffix}"
    with suppress(Exception):
        await _amo_add_tag(deal_id, tag_text)

    # локально отмечаем подтверждение в двух местах (новая и старая схемы)
    pc = state.pending_confirmations.setdefault(deal_id, {})
    conf_map: Dict[str, Set[int]] = pc.setdefault("confirmed", {}) if isinstance(pc.get("confirmed"), dict) else {}  # type: ignore[assignment]
    pc["confirmed"] = conf_map
    conf_map.setdefault(role, set()).add(uid)
    state.confirmed.setdefault(deal_id, set()).add(uid)

    # 2) Перекрашиваем кнопку текущего сообщения
    kb = await _mark_confirmed_on_message_kb(callback, deal_id, role)
    with suppress(Exception):
        if kb:
            await callback.message.edit_reply_markup(reply_markup=kb)

    # 3) Уведомление в общий чат — ГЕНДЕР-НЕЙТРАЛЬНО, ПОЛНЫЕ ДАННЫЕ
    try:
        bot = callback.message.bot if callback.message else None
        if bot:
            chat_id = await _resolve_notify_chat_id(bot)
            if chat_id is not None:
                # Заголовок (название игры)
                title = None
                with suppress(Exception):
                    if callable(globals().get("_deal_title_from_state")):
                        title = globals()["_deal_title_from_state"](deal_id)
                if not title:
                    title = (getattr(state, "deal_titles", {}) or {}).get(deal_id) or "игра"

                # Когда (дата + время) — берём из функции, если она доступна; иначе из state
                when = ""
                with suppress(Exception):
                    if callable(globals().get("_deal_when")):
                        d_s, t_s = globals()["_deal_when"](deal_id)
                        when = f"{(d_s or '').strip()} {(t_s or '').strip()}".strip()
                if not when:
                    raw_when = (getattr(state, "deal_when", {}) or {}).get(deal_id)
                    if isinstance(raw_when, (list, tuple)) and raw_when:
                        when = " ".join([str(x).strip() for x in raw_when if x]).strip()
                    elif isinstance(raw_when, dict):
                        when = " ".join([str(raw_when.get(k, "")).strip() for k in ("date", "time") if raw_when.get(k)]).strip()
                    elif isinstance(raw_when, str):
                        when = raw_when.strip()

                # Роль (человекочитаемая) + эмодзи
                role_human_map = {"main": "Ведущий", "assist": "Помощник", "admin": "Админ"}
                role_emoji_map = {"main": "🎭", "assist": "🤝", "admin": "🛡️"}
                role_human = role_human_map.get(role, "Участник")
                r = role_emoji_map.get(role, "🎯")

                name = human  # короткое «Имя Ф.»

                # Итоговый текст строго по шаблону:
                # ✅ Участие подтверждено: {name} — {r} {role} на «{title}» {when}.
                base = f"✅ Участие подтверждено: {name} — {r} {role_human} на «{title}»"
                text = f"{base} {when}.".strip() if when else f"{base}."

                # Антидубли: одно уведомление на (deal, uid, role)
                notified = state.__dict__.setdefault("_confirm_notified", set())
                key = (deal_id, uid, role)
                if key not in notified:
                    await bot.send_message(chat_id, text)
                    notified.add(key)
            else:
                logger.warning("[confirm] no available chat for notify; skipped")
    except Exception:
        logger.exception("[confirm] notify failed")

    # 4) Если все роли закрыты → статус «Завершение сделки»
    with suppress(Exception):
        all_ok = await _all_required_confirmed(deal_id)
        if all_ok and OK_STATUS_ID:
            moved = await _amo_set_status_success(deal_id)
            logger.info("[confirm] lead %s completed=%s", deal_id, moved)

    # 5) Мягкая перерисовка деталей (если открыты) — без «прыжков»
    with suppress(Exception):
        if callable(refresh_deal_details):
            await refresh_deal_details(bot=callback.message.bot, deal_id=deal_id, force_approved=False)  # type: ignore[misc]

    with suppress(Exception):
        await callback.answer("Готово ✅")

@router.callback_query(lambda c: c.data and c.data.startswith(CONFIRM_PREFIX))
async def confirm_role_handler(callback: CallbackQuery) -> None:
    """
    Кнопки: confirm_role_{deal_id}_{role}
    • Идемпотентно подтверждает участие для main/assist/admin.
    """
    data = str(callback.data or "")
    try:
        _, _, tail = data.partition(CONFIRM_PREFIX)
        lead_s, role_raw = tail.rsplit("_", 1)
        deal_id = int(lead_s)
        role = role_raw.strip().lower()
    except Exception:
        with suppress(Exception):
            await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    if role not in {"main", "assist", "admin"}:
        with suppress(Exception):
            await callback.answer("Неизвестная роль.", show_alert=True)
        return

    await _perform_confirm(callback, deal_id, role)

# История изменений:
# 2025-08-15 — обновлён текст уведомления (гендер-нейтральный шаблон, полные данные: имя, роль+эмодзи, название и дата/время).
#               Логика подтверждения/CRM/перерисовок не изменялась.



# ███ [4.1] CALLBACK: CONFIRM FROM «Мои игры»
# --------------------------------------------------------------------
from typing import Optional
from aiogram.types import CallbackQuery


@router.callback_query(lambda c: c.data and c.data.startswith("mygames_confirm_"))
async def confirm_from_mygames_handler(callback: CallbackQuery) -> None:
    """
    Коллбэк из дашборда: mygames_confirm_{deal_id}
    • Определяем роль пользователя из locked_distribution;
    • Запускаем стандартный пайплайн подтверждения (_perform_confirm);
    • Без автопереходов и «прыжков» UI.
    """
    data = str(callback.data or "")
    try:
        deal_id = int(data.split("_")[-1])
    except Exception:
        await callback.answer("⚠️ Ошибочная кнопка.", show_alert=True)
        return

    uid = int(callback.from_user.id)
    assigned = _assigned_uids_from_locked(deal_id)

    # Приоритет определения роли при коллизиях: admin → main → assist.
    role: Optional[str] = None
    if uid in assigned.get("admin", set()):
        role = "admin"
    elif uid in assigned.get("main", set()):
        role = "main"
    elif uid in assigned.get("assist", set()):
        role = "assist"

    if not role:
        await callback.answer("Роль не найдена или не назначена на вас.", show_alert=True)
        return

    await _perform_confirm(callback, deal_id, role)

# История изменений:
#  • 2025-08-15 — Сохранена логика; типы добавлены для Pylance; прокинут стандартный пайплайн.


# ███ [99] SELF‑TEST (минимальный)
# --------------------------------------------------------------------
async def _test() -> None:
    # to_uid_list
    assert _to_uid_list("Иван И.|101") == [101]
    assert set(_to_uid_list(["101", 202, "Петр|303"])) == {101, 202, 303}

    # role alias
    assert _role_alias("lead1") == "main"
    assert _role_alias("assistant2") == "assist"
    assert _role_alias("admin") == "admin"

    # assigned_uids: смешанный формат (списки + слоты)
    state.locked_distribution = {
        1: {"main": [101], "assistant1": "Иван|202", "admin": "Петр|303"},
    }
    a = _assigned_uids_from_locked(1)
    assert a["main"] == {101} and a["assist"] == {202} and a["admin"] == {303}

    # confirmed_from_state старый/новый формат
    state.pending_confirmations = {
        1: {"confirmed": {"main": {101}, "assist": {202}, "admin": {303}}}
    }
    c = _confirmed_from_state(1)
    assert c["main"] == {101} and c["assist"] == {202} and c["admin"] == {303}

    print("handlers.confirmations ✅ tests passed")


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений [99]: 2025‑08‑13 — расширен self‑test на смешанные форматы
