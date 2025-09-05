# handlers/my_games.py — дашборд «Мои игры»
# ─────────────────────────────────────────────────────────────────────────────
"""
Версия 5.4 · 2025-08-18 (merge)

Что внутри (против 5.3 от 2025-08-13):
• Сохранён весь рабочий функционал старой версии (видимость, кнопки, детали).
• Пылесос сделан более совместимым: поддерживает разные сигнатуры ядра
  delete_previous_private_messages(...) и дополнительно подметает state.detail_blocks.
• Обработчик «Замены» оставлен shim'ом (как в рабочей версии), чтобы не дублировать
  логику из polls_lifecycle и не ловить гонки.

Логика, формат, публичные API и тексты — без изменений.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import re
import contextlib
import logging
import unicodedata
import inspect
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Set, Any
from core.menu import get_menu_message_id  # NEW: не сносим сообщение главного меню

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.state import state
from core.utils import truncate, delete_previous_private_messages, resolve_notify_chat_id  # SSOT-резолвер чата
from services.amocrm import (
    get_amocrm_deals,
    update_amocrm_tags,   # для удаления подтверждающих тегов при замене (используется в чужом пайплайне)
    update_deal_status,   # перевод сделки обратно в «Бронь» (используется в чужом пайплайне)
)

# (детали из отчёта могут понадобиться в будущем; импорт оставляем совместимым)
try:
    from handlers.poll_details import refresh_deal_details  # noqa: F401
except Exception:
    refresh_deal_details = None  # type: ignore

# Префикс подтверждения должен совпадать с handlers.confirmations.py
try:
    from handlers.confirmations import CONFIRM_PREFIX  # ожидается "confirm_role_"
except Exception:  # fallback на ранних сборках
    CONFIRM_PREFIX = "confirm_role_"

# (необязательно) получим ФИО из базы, если доступно — для "Имя Ф."
try:
    from core.db import get_user_info  # type: ignore
except Exception:  # pragma: no cover
    get_user_info = None  # type: ignore

logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")

# ── спецсимволы ──────────────────────────────────────────────────────
NBSP = "\u00A0"     # non-breaking space
FE0F = "\uFE0F"     # emoji variation selector
ZWNBSP = "\uFEFF"   # zero-width no-break space (BOM)

# ── статус-ID ────────────────────────────────────────────────────────
# «Бронь» — требуется подтверждение состава
BRON_STATUS_ID: str = str(getattr(settings, "BRON_STATUS_ID", "18913933") or "18913933")
# «Завершение сделки»
OK_STATUS_ID: str = str(getattr(settings, "SUCCESSFUL_STATUS_ID", "0") or "0")

# ── callback-префиксы ────────────────────────────────────────────────
DETAILS_PREFIX = "mygame_details_"
REPORT_PREFIX  = "mygame_report_"
SWAP_PREFIX    = "mygame_swap_"
RESPOND_PREFIX = "swap_accept_"   # кнопка в общем чате «Откликнуться»

# «Предварительная заявка» — допускается к назначению и подтверждениям
PRELIM_STATUS_ID: str = str(
    getattr(settings, "PRELIM_STATUS_ID", getattr(settings, "PRELIMINARY_STATUS_ID", "")) or ""
)

# История изменений [1]:
# 2025-08-19 — импортирован SSOT-резолвер resolve_notify_chat_id из core.utils; остальное без изменений.




# ════════════════════════════════════════════════════════════════════
# [2] TEXT HELPERS
# ════════════════════════════════════════════════════════════════════
def _log_text(prefix: str, text: str | None) -> None:
    if logger.isEnabledFor(logging.INFO):
        cps = " ".join(f"U+{ord(c):04X}" for c in (text or ""))
        logger.info("[my_games:%s] raw=%r cps=%s", prefix, text, cps)


def _normalize_btn(text: str | None) -> str:
    """Удаляем NBSP/FE0F/ZWNBSP и любые символы категории Cf, приводим к lower()."""
    if not text:
        return ""
    trimmed = (
        text.replace(NBSP, " ")
            .replace(FE0F, "")
            .replace(ZWNBSP, "")
            .strip()
            .lower()
    )
    return "".join(ch for ch in trimmed if unicodedata.category(ch) != "Cf")


def _is_my_games_btn(text: str | None) -> bool:
    _log_text("received", text)
    norm = _normalize_btn(text)
    ok = "мои" in norm and "игры" in norm
    logger.info("[my_games:match] %s => %s", norm, ok)
    return ok


# ███ [2.1] SHOW MY-GAME DETAILS — CRM + утверждённый состав + «✅ Подтвердить»/«Замена»
# --------------------------------------------------------------------
def _tag_uid(tag: Optional[str]) -> Optional[int]:
    """user_id из тега «Имя.Суффикс|123»."""
    if not tag or "|" not in str(tag):
        return None
    try:
        return int(str(tag).rsplit("|", 1)[-1])
    except Exception:
        return None


def _tag_label(tag: Optional[str]) -> str:
    """Человекочитаемая часть тега до «|uid»."""
    if not tag:
        return ""
    s = str(tag)
    return s.split("|", 1)[0].strip()


def _sorted_slots(dist: Dict[str, str], prefix: str) -> List[str]:
    """Возвращает список ключей слотов (lead*/assistant*) в порядке по индексу."""
    slots = [k for k in dist.keys() if isinstance(k, str) and k.startswith(prefix)]

    def keyf(k: str) -> int:
        m = re.search(r"(\d+)$", k)
        return int(m.group(1)) if m else 0

    return sorted(slots, key=keyf)


def _is_locally_confirmed(deal_id: int, uid: int) -> bool:
    """
    Проверка локального подтверждения из state.pending_confirmations:
    • новая схема: dict по ролям {'main': {uid}, ...}
    • старая схема: set {uid, ...}
    """
    pc = getattr(state, "pending_confirmations", {}) or {}
    node = pc.get(deal_id) or {}
    conf = node.get("confirmed")
    if isinstance(conf, dict):
        return any(int(uid) in set(map(int, conf.get(k, set()))) for k in ("main", "assist", "admin"))
    if isinstance(conf, set):
        return int(uid) in set(map(int, conf))
    return False


@router.callback_query(lambda c: c.data and c.data.startswith("mygame_details_"))
async def show_my_game_details(callback: types.CallbackQuery) -> None:
    """
    Детали игры в «🎲 Мои игры»:
      • подробности из CRM (дата, время, место, пакет, игроки, статус);
      • утверждённый состав из locked_distribution (слоты lead*/assistant*/admin/trainee),
        с фолбэком на finished_locked_distribution и snapshot из distribution_cache;
      • кнопка «✅ Подтвердить участие» — при статусе «Бронь» ИЛИ «Предварительная заявка», если назначен;
      • кнопка «🔁 Замена» — после ЛИЧНОГО подтверждения пользователя (даже если вся команда ещё не подтвердила),
        а также при статусе «Завершение сделки»;
      • кнопка «📝 Написать отчёт» — после наступления даты и времени игры.
      • повторно кнопку подтверждения не показываем, если подтверждение уже учтено локально/по тегам.
    """
    # — фильтр против автоперехода из дашборда — только details
    if not str(callback.data).startswith("mygame_details_"):
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    bot = Bot.get_current()
    uid = callback.from_user.id

    # Предварительная уборка sticky-дашборда — чтобы он не «улетал вверх»
    with contextlib.suppress(Exception):
        vac = globals().get("_vacuum_safe")
        if callable(vac):
            coro = vac(int(uid), ignore_sticky=True)  # type: ignore[misc]
            if hasattr(coro, "__await__"):
                await coro

    try:
        deal_id = int(str(callback.data).rsplit("_", 1)[-1])
    except Exception:
        with contextlib.suppress(Exception):
            await callback.answer("Некорректные данные.", show_alert=True)
        return

    # 1) CRM-данные из текущей выборки опроса
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == deal_id), None)

    # 2) Фолбэк: игры, уже отрисованные пользователю
    if not deal:
        user_list = (getattr(state, "games_by_user", {}) or {}).get(uid) or []
        deal = next((d for d in user_list if int(d.get("id", 0)) == deal_id), None)

    # 3) Жёсткий фолбэк из локального кэша/слотов
    if not deal:
        details = (getattr(state, "poll_details", {}) or {}).get(deal_id) or {}
        title = details.get("title") or (getattr(state, "deal_titles", {}) or {}).get(deal_id) or f"Сделка #{deal_id}"
        deal = {
            "id": deal_id,
            "name": title,
            "event_datetime": details.get("event_datetime"),
            "event_time": details.get("event_time") or "—",
            "status_id": BRON_STATUS_ID,
            "address": details.get("address") or details.get("location") or "—",
            "package": details.get("package") or "—",
            "players": details.get("players") or "—",
            "tags": details.get("tags") or [],
            "comment": details.get("comment") or "",
        }

    name = str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal_id}")
    event_dt = deal.get("event_datetime")
    date_s = event_dt.strftime("%d.%m.%Y") if hasattr(event_dt, "strftime") else str(deal.get("event_date") or "—")

    # ВРЕМЯ: приоритет у текстового event_time (нормализуем до HH:MM), затем — из event_datetime (если не "00:00")
    try:
        _norm = globals().get("_normalize_time_str")
        time_s = _norm(str(deal.get("event_time") or "")) if callable(_norm) else str(deal.get("event_time") or "").replace(".", ":").strip()
    except Exception:
        time_s = str(deal.get("event_time") or "").replace(".", ":").strip()
    if not time_s and hasattr(event_dt, "strftime"):
        t_from_dt = event_dt.strftime("%H:%M")
        time_s = "" if t_from_dt == "00:00" else t_from_dt
    if not time_s:
        time_s = "—"

    place = str(deal.get("address") or deal.get("location") or "—")
    pkg_raw = str(deal.get("package") or "—")
    players = truncate(str(deal.get("players") or "—"), 60)
    status_id = str(deal.get("status_id") or "")
    bron_id = str(BRON_STATUS_ID)
    ok_id = str(OK_STATUS_ID)

    # определяем «предварительную заявку» по ID или названию статуса
    status_name_raw = str(deal.get("status_name") or deal.get("status") or "").strip().lower()
    prelim = (PRELIM_STATUS_ID and status_id == str(PRELIM_STATUS_ID)) or (status_name_raw in {"предварительная заявка", "предварительно", "предварит."})

    status = str(
        deal.get("status_name")
        or ("Предварительная заявка" if prelim else ("Бронь" if status_id == bron_id else "Завершение сделки" if status_id == ok_id else "—"))
    )

    # 🔧 Состав: locked → finished_locked → snapshot(distribution_cache)
    dist: Dict[str, Any] = {}
    locked_all = (getattr(state, "locked_distribution", {}) or {})
    finished_all = (getattr(state, "finished_locked_distribution", {}) or {})
    # 1) active locked
    cand = locked_all.get(deal_id) or locked_all.get(str(deal_id))
    if isinstance(cand, dict) and cand:
        dist = cand
    else:
        # 2) finished locked
        cand = finished_all.get(deal_id) or finished_all.get(str(deal_id))
        if isinstance(cand, dict) and cand:
            dist = cand
        else:
            # 3) snapshot (последний известный состав)
            snap = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id)) or {}
            if isinstance(snap, dict) and snap:
                dist = snap

    lead_keys = _sorted_slots(dist, "lead")
    asst_keys = _sorted_slots(dist, "assistant")

    mains_text = ", ".join(filter(None, [_tag_label(dist.get(k)) for k in lead_keys])) or "—"
    assists_text = ", ".join(filter(None, [_tag_label(dist.get(k)) for k in asst_keys])) or "—"
    admin_text = _tag_label(dist.get("admin")) or "—"
    trainee_text = _tag_label(dist.get("trainee"))  # может быть пустым

    # ⬇️ собираем строки деталей с «активными» полями AmoCRM, кроме финансов
    lines = [
        f"🎮 *{name}*",
        f"📅 {date_s} · 🕒 {time_s}",
        f"📍 {place}",
        f"📦 Пакет: {pkg_raw}",
        f"👥 Игроки: {players}",
    ]
    age = str(deal.get("age") or "").strip()
    if age:
        lines.append(f"🎂 Возраст: {age}")
    extra_services = str(deal.get("extra_services") or "").strip()
    if extra_services:
        lines.append(f"➕ Услуги: {extra_services}")
    photographer = str(deal.get("photographer") or "").strip()
    if photographer:
        lines.append(f"📸 Фотограф: {photographer}")
    comment = str(deal.get("comment") or "").strip()
    if comment:
        lines.append(f"💬 Комментарий: {truncate(comment, 700)}")

    lines.append(f"📌 Статус CRM: {status}")
    lines.append("— — —")
    lines.append(f"🧭 Ведущие: {mains_text}")
    lines.append(f"🛟 Помощники: {assists_text}")
    lines.append(f"🛡️ Админ: {admin_text}")
    if trainee_text:
        lines.append(f"🎓 Стажёр: {trainee_text}")

    msgs: List[types.Message] = []
    msgs.append(await bot.send_message(uid, "\n".join(lines), parse_mode="Markdown"))

    # Роль пользователя (для кнопок): prefer union helper, fallback на legacy
    role: Optional[str] = None
    try:
        _role_union = globals().get("_assigned_role_via_locked")
        if callable(_role_union):
            role = _role_union(uid, deal_id)  # type: ignore[misc]
    except Exception:
        role = None
    if role is None:
        try:
            role = _assigned_role_from_state(uid, deal_id)  # legacy helper из файла
        except Exception:
            role = None

    # confirmed по факту: теги ИЛИ локальный state.pending_confirmations
    confirmed_by_tags = _has_confirmation_tag(deal, uid)
    confirmed_local = _is_locally_confirmed(deal_id, uid)
    confirmed = confirmed_by_tags or confirmed_local

    can_confirm = (role in {"main", "assist", "admin"}) and ((status_id == bron_id) or prelim) and (not confirmed)
    # «Замена» после подтверждения или при статусе «Завершение сделки»
    can_swap = (role in {"main", "assist", "admin"}) and (status_id == ok_id)

    # Доступность «Отчёта»
    report_ready = False
    with contextlib.suppress(Exception):
        if hasattr(event_dt, "tzinfo"):
            now = datetime.now(event_dt.tzinfo)
        else:
            now = datetime.now(MSK_TZ)
        if hasattr(event_dt, "strftime"):
            report_ready = now >= event_dt

    # Второе сообщение с кнопками
    second_rows: List[List[InlineKeyboardButton]] = []
    if can_confirm:
        # подтверждение из «Моих игр» идёт через общий handlers.confirmations:
        second_rows.append([
            InlineKeyboardButton(
                text="✅ Подтвердить участие",
                callback_data=f"{CONFIRM_PREFIX}{deal_id}_{role}"
            )
        ])

    if can_swap and not can_confirm:
        second_rows.append([InlineKeyboardButton(text="🔁 Замена", callback_data=f"{SWAP_PREFIX}{deal_id}")])
    if report_ready:
        second_rows.append([InlineKeyboardButton(text="📝 Написать отчёт", callback_data=f"{REPORT_PREFIX}{deal_id}")])

    if second_rows:
        second_kb = InlineKeyboardMarkup(inline_keyboard=second_rows)
        msgs.append(await bot.send_message(uid, "\u2060", reply_markup=second_kb))

    # Сохраняем всю пачку для будущего vacuum
    try:
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = msgs
    except Exception:
        logger.debug("[my_games] failed to record last_user_messages for uid=%s", uid)

    with contextlib.suppress(Exception):
        await callback.answer()

# История изменений [2.1]:
# • 2025-08-29 — Исправлено отображение времени: приоритет event_time (нормализация до HH:MM), 
#                затем — из event_datetime (игнор «00:00»), иначе «—». В остальном без изменений.
# • 2025-08-31 — Выровнено под SSOT: «🔁 Замена» доступна после личного подтверждения или при SUCCESS;
#                сохранён мягкий vacuum sticky; без изменений публичных API.
# • 2025-09-02 — FIX: состав для деталей ищется в locked → finished_locked → distribution_cache (snapshot),
#                роль для кнопок берётся через union-хелпер (если есть); выровнено под SSOT/фиксы Pylance.
# • 2025-09-03 — ВАЖНЫЙ ФИКС: хэндлер матчится только на «mygame_details_…», чтобы не перехватывать
#                колбэки «mygame_swap_…»; иначе кнопка «Замена» не срабатывала.


# ════════════════════════════════════════════════════════════════════
# [2.2] ЗАМЕНА: кнопка «🔁 Замена» из «Мои игры»
# Версия 2.3.0 · 2025-09-03 (SSOT: делегирование в polls_lifecycle; фиксы префиксов/роутера)
# ────────────────────────────────────────────────────────────────────
# Минимальные правки:
# • Убрана локальная реализация замены и повторные префиксы — используем глобальные
#   SWAP_PREFIX="mygame_swap_" и RESPOND_PREFIX="swap_accept_" из файла.
# • НЕ переопределяем router (используем общий router модуля).
# • Делегируем логику в централизованные обработчики из handlers.polls_lifecycle,
#   чтобы не дублировать SSOT (удаление тегов, точечная чистка слотов, уведомления в чат и т.п.).

from aiogram import types
import contextlib
import logging

logger = logging.getLogger(__name__)

@router.callback_query(lambda c: c.data and c.data.startswith(SWAP_PREFIX))
async def mygame_swap_shim(callback: types.CallbackQuery) -> None:
    """Shim: обработка «🔁 Замена» делегируется в polls_lifecycle.swap_request_handler."""
    try:
        from handlers.polls_lifecycle import swap_request_handler as _swap_impl  # type: ignore
        await _swap_impl(callback)
    except Exception as e:
        logger.error("[my_games.swap] delegate failed: %s", e)
        with contextlib.suppress(Exception):
            await callback.answer("⚠️ Не удалось запросить замену. Попробуйте ещё раз.", show_alert=True)

@router.callback_query(lambda c: c.data and c.data.startswith(RESPOND_PREFIX))
async def mygame_swap_accept_shim(callback: types.CallbackQuery) -> None:
    """Shim: обработка «Откликнуться» делегируется в polls_lifecycle.swap_accept_handler."""
    try:
        from handlers.polls_lifecycle import swap_accept_handler as _accept_impl  # type: ignore
        await _accept_impl(callback)
    except Exception as e:
        logger.error("[my_games.swap_accept] delegate failed: %s", e)
        with contextlib.suppress(Exception):
            await callback.answer("⚠️ Кнопка недоступна. Возможно, замена уже найдена.", show_alert=True)

# История изменений [2.2]:
# • 2025-09-03 — 2.3.0: выровнено под SSOT — делегирование в polls_lifecycle; убраны локальные префиксы/роутер.



# ════════════════════════════════════════════════════════════════════
# [3] DOMAIN HELPERS
# ════════════════════════════════════════════════════════════════════
def _safe_event_dt(deal: Dict) -> Optional[datetime]:
    dt = deal.get("event_datetime")
    return dt if isinstance(dt, datetime) else None


def _safe_title(deal: Dict) -> str:
    return (deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}").strip()


def _safe_status_id(deal: Dict) -> str:
    return str(deal.get("status_id") or "")


def _is_user_assigned_legacy(uid: int, deal: Dict) -> bool:
    """Legacy-назначение: пользователь есть в team_leads у сделки из CRM (доп. источник)."""
    leads = deal.get("team_leads") or []
    uid_s = str(uid)
    for t in leads:
        try:
            if str(t.get("id")) == uid_s:
                return True
        except Exception:
            continue
    return False


def _is_main_leader_legacy(uid: int, deal: Dict) -> bool:
    """Legacy: первый из team_leads считается основным."""
    leads = deal.get("team_leads") or []
    try:
        return bool(leads and str(leads[0].get("id")) == str(uid))
    except Exception:
        return False


def _short_name(uid: int) -> str:
    """
    Короткое имя "Имя Ф." (с точкой).
    Берём из core.db.get_user_info (если СИНХРОННАЯ функция) или state.users.
    """
    if callable(get_user_info) and not inspect.iscoroutinefunction(get_user_info):
        try:
            u = get_user_info(uid)  # sync путь
            if isinstance(u, dict):
                fn = (u.get("first_name") or "").strip()
                li = (u.get("last_name_initial") or "").strip()
                if fn and li:
                    return f"{fn} {li}."
                if fn:
                    return fn
        except Exception:
            pass
    users_map: Dict[int, Dict[str, Any]] = getattr(state, "users", {}) or {}
    u = users_map.get(uid) or {}
    fn = (u.get("first_name") or "").strip()
    li = (u.get("last_name_initial") or "").strip()
    if fn and li:
        return f"{fn} {li}."
    if fn:
        return fn
    return ""


def _expected_tags_for(uid: int) -> Set[str]:
    """
    Набор тегов подтверждения для пользователя:
    БАЗА:  'Имя Ф.1', 'Имя Ф.2', 'Имя Ф.Адм'  (новый корректный формат, без лишней точки).
    Совместимость: принимаем и старые варианты — с точкой перед суффиксом и «двойной точкой».
    """
    base = _short_name(uid)
    if not base:
        return set()
    # новый формат (нормальный)
    tags: Set[str] = {f"{base}1", f"{base}2", f"{base}Адм"}
    # старый формат (с точкой перед суффиксом)
    tags |= {f"{base}.1", f"{base}.2", f"{base}.Адм"}
    # исторические вариации/опечатки
    tags |= {f"{base} .Адм", f"{base}. Адм", f"{base}.Ад", f"{base}. Ад"}
    # «двойная точка» встречалась ранее («Имя Ф.» + «.1»)
    tags |= {f"{base}..1", f"{base}..2", f"{base}..Адм"}
    return tags



def _has_confirmation_tag(deal: Dict, uid: int) -> bool:
    tags = {str(t.get("name")) for t in (deal.get("tags") or []) if isinstance(t, dict) and t.get("name")}
    need = _expected_tags_for(uid)
    return bool(tags & need)


def _label_belongs_to_uid(slot_value: Optional[str], uid: int) -> bool:
    """
    Надёжная проверка «этот слот про данного пользователя», поддерживает:
      • новый формат: 'Имя Ф..1|123' — по |uid,
      • фолбэк:       'Имя Ф..1'     — по ярлыку (сопоставляется с коротким именем).
    """
    if not slot_value or not isinstance(slot_value, str):
        return False

    # 1) если в слоте есть |uid — проверяем его
    tuid = _tag_uid(slot_value)
    if tuid is not None:
        return int(tuid) == int(uid)

    # 2) фолбэк по ярлыку слота и короткому имени
    label = _tag_label(slot_value)
    base = _short_name(uid)
    if not label or not base:
        return False

    # нормализуем пробелы/невидимые символы
    def _norm(s: str) -> str:
        s = s.replace(NBSP, " ").replace(FE0F, "").replace(ZWNBSP, "")
        s = " ".join(s.split()).strip()
        return s

    return _norm(label).lower().startswith(_norm(base).lower())


def _assigned_role_from_state(uid: int, deal_id: int) -> Optional[str]:
    """
    Определяет роль пользователя в зафиксированном составе (после «Утвердить»):
    • новый формат: слоты lead*/assistant*/admin → строки «Имя Ф.<суффикс>|uid» или «Имя Ф.<суффикс>» (фолбэк);
    • legacy формат: списки int по ключам main/assist/admin.
    Возвращает 'main' | 'assist' | 'admin' | None.
    """
    roles = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
            or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
            or {}

    # Новый формат (слоты)
    if any(isinstance(k, str) and k.startswith("lead") for k in roles.keys()) or \
       (isinstance(roles.get("admin"), str)):
        for k in _sorted_slots(roles, "lead"):
            if _label_belongs_to_uid(roles.get(k), uid):
                return "main"
        for k in _sorted_slots(roles, "assistant"):
            if _label_belongs_to_uid(roles.get(k), uid):
                return "assist"
        if _label_belongs_to_uid(roles.get("admin"), uid):
            return "admin"
        return None

    # Legacy формат (списки int)
    main = set(map(int, roles.get("main", []) or []))
    assist = set(map(int, roles.get("assist", []) or []))
    admin = set(map(int, roles.get("admin", []) or []))
    if uid in main:
        return "main"
    if uid in assist:
        return "assist"
    if uid in admin:
        return "admin"
    return None


def _is_user_assigned_current(uid: int, deal: Dict) -> bool:
    """
    Пользователь назначен на сделку:
    • утверждён руководителем (state.assigned_index[uid] содержит deal_id), ИЛИ
    • есть роль в locked_distribution (новый/legacy), ИЛИ
    • legacy — указан в team_leads, ИЛИ
    • есть тег подтверждения (факт).
    """
    did = int(deal.get("id") or 0)
    assigned_index: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
    if did and uid in assigned_index and did in (assigned_index.get(uid) or set()):
        return True
    if _assigned_role_from_state(uid, did):
        return True
    if _is_user_assigned_legacy(uid, deal):
        return True
    if _has_confirmation_tag(deal, uid):
        return True
    return False


def _details_text(deal: Dict, confirmed: bool) -> str:
    dt = _safe_event_dt(deal)
    date_part = dt.strftime("%d.%m.%Y") if dt else "—"
    status_txt = "✅ Подтверждено" if confirmed else "⏳ Ожидает подтверждения"
    time_txt = deal.get("event_time", "—")
    package = deal.get("package", "—")
    players = deal.get("players", "—")
    comment = deal.get("comment", "—")
    title = _safe_title(deal)
    return (
        f"🎮 *{title}*\n"
        f"📅 *Дата*: {date_part}\n"
        f"🕒 *Время*: {time_txt}\n"
        f"📦 *Пакет*: {package}\n"
        f"👥 *Игроки*: {players}\n"
        f"🔖 *Статус*: {status_txt}\n"
        f"💬 *Комментарий*: {comment}"
    )


def _bron_status_id() -> str:
    """Безопасный резолвер ID статуса «Бронь»."""
    try:
        return str(BRON_STATUS_ID)  # noqa: F821
    except Exception:
        return str(getattr(settings, "BRON_STATUS_ID", ""))


def _details_kb(uid: int, deal: Dict, confirmed: bool) -> InlineKeyboardMarkup:
    """
    Фолбэк-клавиатура (когда недоступна расширенная разметка).
    Кнопка «✅ Подтвердить участие» активна только для статуса «Бронь»,
    когда пользователь назначен по зафиксированному распределению и ещё не подтвердил.
    """
    kb = InlineKeyboardBuilder()

    role = _assigned_role_from_state(uid, int(deal.get("id") or 0))
    can_confirm = (
        _safe_status_id(deal) == _bron_status_id()
        and (role in {"main", "assist", "admin"})
        and not confirmed
    )

    if can_confirm:
        kb.button(
            text="✅ Подтвердить участие",
            callback_data=f"{CONFIRM_PREFIX}{deal['id']}_{role}",
        )

    if _is_main_leader_legacy(uid, deal):
        kb.button(text="📝 Написать отчёт", callback_data=f"{REPORT_PREFIX}{deal['id']}")

    if _is_user_assigned_current(uid, deal):
        kb.button(text="🔄 Попросить замену", callback_data=f"{SWAP_PREFIX}{deal['id']}")

    kb.button(text="← Назад", callback_data="mygames_back")
    kb.adjust(1)
    return kb.as_markup()

# История изменений [3]:
# • 2025-08-19 — добавлен _label_belongs_to_uid и использован в _assigned_role_from_state;
#                теперь «Мои игры» видят слоты без суффикса |uid (фолбэк по ярлыку).
# • 2025-08-20 — _short_name возвращает строго «Имя Ф.» (точка после инициалов).


# ════════════════════════════════════════════════════════════════════
# [3.1] SWAP HELPERS (используются внешними пайплайнами; оставляем)
# ════════════════════════════════════════════════════════════════════
def _find_deal_snapshot(deal_id: int) -> Dict[str, Any]:
    """
    Возвращает «снимок» сделки из возможных источников: games_by_user, current_poll_deals, CRM.
    Нужен для формирования текста и работы с тегами.
    """
    # 1) локальный кэш у пользователей
    for deals in (getattr(state, "games_by_user", {}) or {}).values():
        d = next((x for x in deals if int(x.get("id") or 0) == deal_id), None)
        if d:
            return d
    # 2) текущая выборка опроса
    d = next((x for x in (getattr(state, "current_poll_deals", []) or []) if int(x.get("id") or 0) == deal_id), None)
    if d:
        return d
    # 3) CRM полный список
    try:
        all_deals = state._last_deals_cache  # возможен быстрый кэш, если уже тянули ранее
    except Exception:
        all_deals = None
    try:
        if not all_deals:
            all_deals = asyncio.run(get_amocrm_deals())  # fallback sync-context (редкий путь)
    except Exception:
        all_deals = []
    return next((x for x in (all_deals or []) if int(x.get("id") or 0) == deal_id), {})  # может быть пустой


def _clear_assigned_slot(deal_id: int, role: str, uid: int) -> None:
    """
    Очищает соответствующий слот в locked_distribution и удаляет deal_id из assigned_index[uid].
    """
    locked = (getattr(state, "locked_distribution", {}) or {})
    dist: Dict[str, Any] = locked.get(deal_id) or locked.get(str(deal_id)) or {}
    if not isinstance(dist, dict):
        dist = {}

    # слоты нового формата
    if role == "main":
        for k in list(dist.keys()):
            if isinstance(k, str) and k.startswith("lead") and _tag_uid(dist.get(k)) == uid:
                dist[k] = ""
    elif role == "assist":
        for k in list(dist.keys()):
            if isinstance(k, str) and k.startswith("assistant") and _tag_uid(dist.get(k)) == uid:
                dist[k] = ""
    elif role == "admin":
        if _tag_uid(dist.get("admin")) == uid:
            dist["admin"] = ""

    # сохранить обратно (по тому же ключу типа)
    if deal_id in locked:
        locked[deal_id] = dist
    elif str(deal_id) in locked:
        locked[str(deal_id)] = dist

    # assigned_index
    try:
        aidx: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
        cur = set(aidx.get(uid) or set())
        if deal_id in cur:
            cur.discard(deal_id)
            aidx[uid] = cur
    except Exception:
        pass


def _filter_tags_without_user(tags: List[Dict[str, Any]] | List[Any], uid: int) -> List[str]:
    """
    Возвращает новый список имён тегов БЕЗ подтверждающих тегов указанного пользователя.
    На вход подаём любые структуры тегов из CRM (list[dict{name}] или list[str]).
    """
    names: Set[str] = set()
    for t in tags or []:
        if isinstance(t, dict) and t.get("name"):
            names.add(str(t.get("name")))
        elif isinstance(t, str):
            names.add(t)

    kill = _expected_tags_for(uid)
    return [n for n in names if n not in kill]

# ════════════════════════════════════════════════════════════════════
# [3.2] Мои игры — состояние подтверждения и кнопка
# Версия 3.2.4 · 2025-08-28
# Изменения:
# • Подтверждение НЕ перехватывается этим модулем: формируем callback строго под handlers/confirmations.
# • Кнопка «Замена» показывается только если пользователь уже подтвердил ИЛИ сделка в «Завершении».
# • Фолбэк-проверки тегов/локальной отметки остались без изменений.
# ════════════════════════════════════════════════════════════════════
from contextlib import suppress
from typing import Any, Dict, Iterable, Optional
from aiogram.types import InlineKeyboardButton

from core.state import state
from core.utils import to_uid_list, normalize_roles

# CRM-обёртка — используем, если доступна
try:
    from services import amocrm as _amo  # type: ignore
except Exception:
    _amo = None  # type: ignore


def _mg_role_alias(key: str) -> str:
    k = (key or "").lower()
    if k.startswith("lead") or k == "main": return "main"
    if k.startswith("assist"):              return "assist"
    if "admin" in k:                        return "admin"
    if "trainee" in k or "intern" in k or "стаж" in k: return "trainee"
    return k


def _mg_label_for_user_from_locked(deal_id: int, uid: int) -> Optional[str]:
    """
    Возвращает подпись из locked_distribution ДО '|uid' — строго как зафиксировано.
    Предпочтительно используем слоты, где подпись уже с «.1/.2/.Адм/.Стаж».
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
       or {}

    # слоты нового формата
    if isinstance(raw, dict) and raw:
        for slot, val in raw.items():
            role = _mg_role_alias(slot)
            seq: Iterable[Any] = val if isinstance(val, (list, tuple, set)) else [val]
            for item in seq:
                s = str(item or "").strip()
                if not s:
                    continue
                try:
                    uids = set(map(int, to_uid_list(s)))
                except Exception:
                    uids = set()
                if int(uid) in uids:
                    return s.split("|", 1)[0].strip()
        return None

    # ролевая форма (редкие ранние сборки) — восстановим подпись «Имя Ф.» из кэша «Имя|uid»
    try:
        roles = normalize_roles(raw)
        for bucket in (roles.get("main") or []) + (roles.get("assist") or []) + (roles.get("admin") or []):
            if int(bucket) == int(uid):
                dc = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
                if isinstance(dc, dict):
                    for v in dc.values():
                        vs = str(v or "")
                        if vs.endswith(f"|{uid}"):
                            return vs.split("|", 1)[0].strip()
    except Exception:
        pass
    return None


async def _mg_crm_confirmation_tags(deal_id: int) -> set[str]:
    """Командные теги из CRM по сделке (Set[str]); на ошибках — пустое множество."""
    tags: set[str] = set()
    if not _amo:
        return tags
    with suppress(Exception):
        if hasattr(_amo, "get_deal_by_id"):
            deal = await _amo.get_deal_by_id(int(deal_id))  # type: ignore[arg-type]
            for t in (deal or {}).get("tags") or []:
                name = str((t or {}).get("name") or "").strip()
                if name:
                    tags.add(name)
    return tags


async def _mg_is_user_confirmed_for_deal(uid: int, deal_id: int) -> bool:
    """
    True, если у пользователя уже есть подтверждение:
      • в CRM-тегах присутствует точная подпись из locked_distribution;
      • фолбэк: локальный кэш state.pending_confirmations/confirmed.
    """
    label = _mg_label_for_user_from_locked(deal_id, uid)
    if label:
        tags = await _mg_crm_confirmation_tags(deal_id)
        if tags and label in tags:
            return True

    # локальный фолбэк
    pc = (getattr(state, "pending_confirmations", {}) or {}).get(deal_id) or {}
    loc = pc.get("confirmed")
    if isinstance(loc, dict):
        for k in ("main", "assist", "admin", "trainee"):
            s = loc.get(k)
            if isinstance(s, set) and int(uid) in s:
                return True
            if s:
                with suppress(Exception):
                    if int(uid) in set(map(int, to_uid_list(s))):
                        return True
    else:
        with suppress(Exception):
            if int(uid) in set((getattr(state, "confirmed", {}) or {}).get(deal_id) or set()):
                return True
    return False


async def build_confirm_button_for_mygame(uid: int, deal_id: int) -> InlineKeyboardButton:
    """
    Возвращает кнопку для карточки «Моих игр»:
      • «🔁 Замена» — если пользователь уже подтвердил участие;
      • иначе «✅ Подтвердить» с callback `confirm_role_{deal_id}_{role}`.
    """
    if await _mg_is_user_confirmed_for_deal(uid, deal_id):
        return InlineKeyboardButton(text="🔁 Замена", callback_data=f"{SWAP_PREFIX}{deal_id}")

    role = _assigned_role_from_state(uid, deal_id)
    if role not in {"main", "assist", "admin"}:
        return InlineKeyboardButton(text="noop", callback_data="noop")

    return InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"{CONFIRM_PREFIX}{deal_id}_{role}")
# История изменений [3.2]:
# • 2025-08-28 — выровнено под SSOT; подтверждение отдаём handlers.confirmations; фиксы Pylance.

# ════════════════════════════════════════════════════════════════════
# [3.3] NOTIFY: announce_if_all_confirmed — заголовок «как в опросе»: Название — Дата Время Пакет Бонусы
# ════════════════════════════════════════════════════════════════════
import logging
from contextlib import suppress
from typing import List, Tuple, Optional

from aiogram import Bot
from core.state import state
from core.utils import team_bulleted_lines, resolve_notify_chat_id
from services import amocrm as _amo  # type: ignore

logger = logging.getLogger(__name__)

def _normalize_time_str(raw: Optional[str]) -> str:
    """
    Приводит строку времени к формату 'HH:MM'.
    • Заменяет точки на двоеточия ('18.00' → '18:00').
    • Дополняет нули ('9' → '09:00', '930' → '09:30').
    • Пустое/мусор → ''.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.replace(".", ":").replace(" ", "")
    if ":" not in s:
        if not s.isdigit():
            return ""
        if len(s) == 4:
            hh, mm = s[:2], s[2:]
        elif len(s) == 3:
            hh, mm = s[:1], s[1:]
        elif len(s) == 2:
            hh, mm = s, "00"
        else:
            hh, mm = s, "00"
        try:
            return f"{int(hh):02d}:{int(mm):02d}"
        except Exception:
            return ""
    try:
        h, m = s.split(":", 1)
        return f"{int(h or 0):02d}:{int(m or 0):02d}"
    except Exception:
        return ""

def _normalize_date_short(raw: Optional[str]) -> str:
    """'YYYY-MM-DD' → 'DD.MM'; 'DD.MM.YYYY' → 'DD.MM'; иначе — как есть."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "-" in s:
        with suppress(Exception):
            y, m, d = s.split("-", 2)
            return f"{int(d):02d}.{int(m):02d}"
    parts = s.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        d, m, _y = parts
        with suppress(Exception):
            return f"{int(d):02d}.{int(m):02d}"
    return s

def _pick_first(d: dict, keys: List[str]) -> str:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""

def _title_date_time(did: int) -> Tuple[str, str, str, str, str]:
    """
    Возвращает (title, date_s, time_s, package_s, bonus_s) для уведомления БЕЗ фолбэка на «Сделка #ID».
    Источники (по порядку):
      • state.current_poll_deals
      • state.deals_index
      • state.distribution_cache[str(id)]
      • state.games_by_user[*]
    Дата → 'DD.MM', время → 'HH:MM'. Если title пуст — не подставляем ID.
    """
    title, date_s, time_s, package_s, bonus_s = "", "", "", "", ""

    def _apply_from(d: dict) -> None:
        nonlocal title, date_s, time_s, package_s, bonus_s
        if not isinstance(d, dict):
            return
        if not title:
            title = str(d.get("game_name") or d.get("name") or d.get("title") or "").strip()

        # дата/время
        dt = d.get("event_datetime")
        if not date_s:
            if hasattr(dt, "strftime"):
                date_s = dt.strftime("%d.%m")
            else:
                date_s = _normalize_date_short(_pick_first(d, ["event_date", "date", "eventDate", "game_date"]))
        # источники времени (широкий список, чтобы совпасть с опросом)
        if not time_s:
            # приоритет: явные time-поля
            time_raw = _pick_first(
                d,
                [
                    "event_time",
                    "time",
                    "time_start",
                    "start_time",
                    "slot_time",
                    "begin_time",
                    "game_time",
                    "startAt",
                    "start_at",
                    "start",
                ],
            )
            if time_raw:
                time_s = _normalize_time_str(time_raw)
            elif hasattr(dt, "strftime"):
                t_dt = dt.strftime("%H:%M")
                # Если время в dt = 00:00 — пробуем альтернативные поля-строки
                if t_dt != "00:00":
                    time_s = t_dt

        # пакет/бонусы (как в опросе)
        if not package_s:
            package_s = str(
                _pick_first(d, ["package_human", "package_short", "package", "pkg", "tariff"])
            ).strip()
        if not bonus_s:
            bonus_s = str(
                _pick_first(d, ["bonus_human", "bonuses", "bonus", "extra_bonuses", "extra_services"])
            ).strip()

    # 1) текущий снапшот опроса
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == did:
                _apply_from(d)
                break

    # 2) deals_index
    if not (title and date_s and time_s and package_s and bonus_s):
        with suppress(Exception):
            meta = (getattr(state, "deals_index", {}) or {}).get(did) \
                or (getattr(state, "deals_index", {}) or {}).get(str(did)) \
                or {}
            _apply_from(meta)

    # 3) distribution_cache (снимок опроса на уровне сделки)
    if not (title and date_s and time_s and package_s and bonus_s):
        with suppress(Exception):
            snap = (getattr(state, "distribution_cache", {}) or {}).get(str(did)) or {}
            _apply_from(snap)

    # 4) games_by_user (как резерв, если отчёт уже подмели)
    if not (title and date_s and time_s and package_s and bonus_s):
        with suppress(Exception):
            for _, arr in (getattr(state, "games_by_user", {}) or {}).items():
                for d in (arr or []):
                    if int(d.get("id") or 0) == did:
                        _apply_from(d)
                        raise StopIteration

    return title, date_s, time_s, package_s, bonus_s

async def _status_info_for(did: int) -> Tuple[Optional[str], str]:
    """
    Возвращает (status_id:str|None, status_name_lower:str)
    Источники: AmoCRM → state.current_poll_deals → deals_index.
    """
    with suppress(Exception):
        if hasattr(_amo, "get_deal_by_id"):
            deal = await _amo.get_deal_by_id(int(did))  # type: ignore[arg-type]
            sid = deal.get("status_id") or deal.get("pipeline_status_id")
            name = str(deal.get("status_name") or deal.get("status") or "").strip().lower()
            return (str(sid) if sid is not None else None, name)

    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(did):
                sid = d.get("status_id") or d.get("pipeline_status_id")
                name = str(d.get("status_name") or d.get("status") or "").strip().lower()
                return (str(sid) if sid is not None else None, name)

    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(did) \
            or (getattr(state, "deals_index", {}) or {}).get(str(did)) \
            or {}
        sid = meta.get("status_id")
        name = str(meta.get("status_name") or meta.get("status") or "").strip().lower()
        return (str(sid) if sid is not None else None, name)

    return (None, "")

async def announce_if_all_confirmed(deal_id: int) -> None:
    """
    Шлёт одноразовое уведомление в рабочий чат, что все назначенные роли подтвердили участие.
    Идемпотентность: на один deal_id — не более одного уведомления за сессию.
    Если подтверждения по ролям ещё не полные — тихо выходим.
    Для «Предварительной заявки» добавляется строка-предупреждение с эмодзи.
    """
    try:
        did = int(deal_id)
    except Exception:
        return

    try:
        # антидубли на время жизни процесса
        announced: set[int] = state.__dict__.setdefault("_all_confirmed_announced", set())  # type: ignore[assignment]
        if did in announced:
            return

        # проверка полноты подтверждений — используем логику из handlers.confirmations
        try:
            from handlers.confirmations import _all_required_confirmed  # type: ignore
        except Exception:
            _all_required_confirmed = None  # type: ignore
        if not callable(_all_required_confirmed) or not await _all_required_confirmed(did):  # type: ignore[misc]
            return

        # состав печатаем по зафиксированному распределению (locked_distribution)
        slots = (
            (getattr(state, "locked_distribution", {}) or {}).get(did)
            or (getattr(state, "locked_distribution", {}) or {}).get(str(did))
            or {}
        )
        if not isinstance(slots, dict) or not slots:
            return
        lines: List[str] = await team_bulleted_lines(slots)

        # заголовок/дата/время/пакет/бонусы (как в опросе; без фолбэка на ID и без лишнего «—»)
        title, date_s, time_s, package_s, bonus_s = _title_date_time(did)
        tail = " ".join(x for x in (date_s, time_s, package_s, bonus_s) if x).strip()
        if title:
            head = f"🎉 {title}" + (f" — {tail}" if tail else "")
        else:
            head = f"🎉 — {tail}" if tail else ""

        # статус (для предупреждения при «Предварительной заявке»)
        sid, name_lower = await _status_info_for(did)
        prelim_id = str(globals().get("PRELIM_STATUS_ID", "") or "")
        is_prelim = (prelim_id and sid and str(sid) == prelim_id) or (
            name_lower in {"предварительная заявка", "предварительно", "предварит."}
        )

        # куда слать
        bot = Bot.get_current()
        try:
            chat_id = resolve_notify_chat_id(bot)  # предпочтительная сигнатура
        except TypeError:
            chat_id = resolve_notify_chat_id()     # фолбэк для старых сборок
        if chat_id is None:
            logger.warning("[my_games] notify chat not resolved; skip announce")
            return

        # текст уведомления
        parts: List[str] = ["✅ Вся команда подтвердила участие."]
        if head:
            parts.append(head)
        if lines:
            parts.append("\n".join(lines))
        if is_prelim:
            parts.append("⚠️ Это *предварительная заявка*. Игра не гарантирована. Ждём предоплату.")
        text = "\n".join(p for p in parts if p)

        await bot.send_message(chat_id, text)
        announced.add(did)
    except Exception as e:
        logger.warning("[my_games] announce_if_all_confirmed failed for deal=%s: %s", deal_id, e)

# История изменений [3.3]:
# • 2025-09-02 — заголовок дополнен временем и бонусами, источники времени расширены (event_time/time/start_time/...),
#                 формат ровно как в опросе: «🎉 Название — ДД.MM HH:MM Пакет Бонусы», без фолбэка «Сделка #ID».


# ════════════════════════════════════════════════════════════════════
# [3.4] Дашборд «Мои игры» — мягкий редрав и кнопки действий
# Версия 3.4.6 · 2025-09-05 (фикс: «✅ Подтверждено» до SUCCESS; «Замена» только на SUCCESS)
# ════════════════════════════════════════════════════════════════════
import logging
from contextlib import suppress
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING, Callable, cast

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.state import state

# Заглушка для Pylance: во время type-check иметь сигнатуру функции из [1.4].
if TYPE_CHECKING:
    def get_my_games_dashboard(uid: int) -> Optional[int]: ...
else:
    # Рантайм-шим: всегда берём актуальную реализацию из globals()
    def get_my_games_dashboard(uid: int) -> Optional[int]:
        f = globals().get("get_my_games_dashboard")
        if f is not None and f is not get_my_games_dashboard:
            try:
                return cast(Callable[[int], Optional[int]], f)(uid)
            except Exception:
                return None
        return None

logger = logging.getLogger(__name__)


def _role_human(role: Optional[str]) -> str:
    m = {"main": "Ведущий", "assist": "Помощник", "admin": "Админ", "trainee": "Стажёр"}
    return m.get((role or "").lower(), "Роль")


def _deal_meta(did: int) -> Tuple[str, str, str, str]:
    """
    Возвращает (title, date_s, time_s, pkg).
    Источники: state.current_poll_deals → state.deals_index.
    """
    title, date_s, time_s, pkg = f"Сделка #{did}", "", "", ""
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(did):
                title = str(d.get("game_name") or d.get("name") or title)
                if d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                    date_s = d["event_datetime"].strftime("%d.%m.%Y")
                    time_s = d["event_datetime"].strftime("%H:%M")
                else:
                    date_s = str(d.get("event_date") or "")
                    time_s = str(d.get("event_time") or "")
                pkg = str(d.get("package") or "")
                return title, date_s, time_s, pkg
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(did) \
            or (getattr(state, "deals_index", {}) or {}).get(str(did)) \
            or {}
        title = str(meta.get("title") or title)
        date_s = str(meta.get("date") or "")
        time_s = str(meta.get("time") or "")
        pkg = str(meta.get("package") or "")
    return title, date_s, time_s, pkg


def _is_locally_confirmed_for_redraw(did: int, uid: int) -> bool:
    """
    Локальная отметка подтверждения (фолбэк, когда CRM-теги ещё не потянулись).
    Используем pending_confirmations/confirmed.
    """
    pc = (getattr(state, "pending_confirmations", {}) or {}).get(int(did), {}) or {}
    with suppress(Exception):
        conf = pc.get("confirmed") or {}
        if isinstance(conf, dict):
            for k in ("main", "assist", "admin", "trainee"):
                s = conf.get(k) or set()
                if isinstance(s, set) and int(uid) in s:
                    return True
        elif isinstance(conf, set):
            return int(uid) in conf
    with suppress(Exception):
        confirmed = (getattr(state, "confirmed", {}) or {}).get(int(did)) or set()
        return int(uid) in confirmed
    return False


def _report_available_for(deal_id: int) -> bool:
    """
    Кнопку «📝 Написать отчёт» показываем после наступления даты/времени игры.
    Берём event_datetime из того же снапшота, что использовали при отрисовке.
    """
    with suppress(Exception):
        for d in (getattr(state, "games_by_user", {}) or {}).get(int(getattr(state, "report_uid_hint", 0)), []):
            if int(d.get("id") or 0) == int(deal_id):
                dt = globals().get("_safe_event_dt", lambda *_: None)(d)  # type: ignore
                if dt:
                    from datetime import datetime as _dt
                    now = _dt.now(dt.tzinfo) if dt.tzinfo else _dt.now()
                    return now >= dt
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(deal_id):
                dt = globals().get("_safe_event_dt", lambda *_: None)(d)  # type: ignore
                if dt:
                    from datetime import datetime as _dt
                    now = _dt.now(dt.tzinfo) if dt.tzinfo else _dt.now()
                    return now >= dt
    return False


def _build_dashboard_kb(uid: int, deals_sorted: List[Dict]) -> InlineKeyboardMarkup:
    """
    Собирает разметку дашборда: на каждую игру — строка деталей и строка действия
    («✅ Подтвердить» / «🔁 Замена»). «📝 Написать отчёт» — отдельной строкой при наступлении даты.
    Логика полностью локальная, без внешних хелперов.
    """
    from core.config import settings as _cfg
    from core.utils import truncate as _trunc

    BRON_ID: str = str(getattr(_cfg, "BRON_STATUS_ID", ""))
    OK_ID: str = str(getattr(_cfg, "SUCCESSFUL_STATUS_ID", ""))
    PRELIM_ID: str = str(getattr(_cfg, "PRELIM_STATUS_ID", getattr(_cfg, "PRELIMINARY_STATUS_ID", "")) or "")

    _safe_title = cast(Callable[[Dict[str, Any]], str], globals().get("_safe_title", lambda d: str(d.get("name") or "")))  # type: ignore
    _safe_event_dt = cast(Callable[[Dict[str, Any]], Optional["datetime"]], globals().get("_safe_event_dt", lambda *_: None))  # type: ignore
    _safe_status_id = cast(Callable[[Dict[str, Any]], str], globals().get("_safe_status_id", lambda d: str(d.get("status_id") or "")))  # type: ignore
    _has_confirmation_tag = cast(Callable[[Dict[str, Any], int], bool], globals().get("_has_confirmation_tag"))  # type: ignore
    _assigned_role_from_state = cast(Callable[[int, int], Optional[str]], globals().get("_assigned_role_from_state"))  # type: ignore

    kb = InlineKeyboardBuilder()

    for d in deals_sorted:
        did = int(d.get("id") or 0)
        title = _trunc(_safe_title(d), 28)
        dt = _safe_event_dt(d)
        # Исправлено: корректный формат даты с латинской 'm'
        date = dt.strftime("%d.%m") if dt else "??.??"

        sid = _safe_status_id(d)
        name = str(d.get("status_name") or d.get("status") or "").strip().lower()
        prelim_names = {"предварительная заявка", "предварительно", "предварит."}
        if (PRELIM_ID and sid == PRELIM_ID) or (name in prelim_names):
            status = "Предвар."
        else:
            status = "Бронь" if sid == BRON_ID else "Заверш."

        # строка с деталями
        kb.button(
            text=f"ℹ️ {title} · {date} · {status}",
            callback_data=f"{globals().get('DETAILS_PREFIX','mygame_details_')}{did}"
        )

        # состояние подтверждения
        confirmed = (_has_confirmation_tag(d, uid) if callable(_has_confirmation_tag) else False) or _is_locally_confirmed_for_redraw(did, uid)  # type: ignore
        role = _assigned_role_from_state(uid, did) if callable(_assigned_role_from_state) else None

        # строка действия (фикс логики)
        if sid == OK_ID:
            kb.row(InlineKeyboardButton(
                text="🔁 Замена",
                callback_data=f"{globals().get('SWAP_PREFIX','mygame_swap_')}{did}"
            ))
        elif confirmed:
            kb.row(InlineKeyboardButton(text="✅ Подтверждено", callback_data="mygame_noop"))
        elif role in {"main", "assist", "admin"} and (sid == BRON_ID or status == "Предвар."):
            kb.row(InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"{globals().get('CONFIRM_PREFIX','confirm_role_')}{did}_{role}"
            ))

        # «Отчёт» — отдельной строкой при наступлении даты/времени
        if _report_available_for(did):  # type: ignore
            kb.row(InlineKeyboardButton(text="📝 Написать отчёт", callback_data=f"{globals().get('REPORT_PREFIX','mygame_report_')}{did}"))

    kb.adjust(1)
    return kb.as_markup()


async def _soft_redraw_my_games(uid: int) -> None:
    """
    МЯГКИЙ редрав «Моих игр»: заменяем только reply_markup.
    Если в кэше нет сообщения — используем sticky-id из [1.4].
    """
    try:
        if (getattr(state, "ui_context", {}) or {}).get(int(uid)) != "my_games":
            logger.debug("[my_games] skip soft redraw: context not my_games (uid=%s)", uid)
            return

        header = (getattr(state, "last_user_messages", {}) or {}).get(int(uid), [None])[0]
        header_mid: Optional[int] = None
        if header and getattr(header, "message_id", None):
            header_mid = int(header.message_id)
        if not header_mid and callable(get_my_games_dashboard):
            # фолбэк к sticky-id
            with suppress(Exception):
                header_mid = get_my_games_dashboard(int(uid))  # type: ignore[misc]

        if not header_mid:
            logger.debug("[my_games] skip soft redraw: no header/sticky message id")
            return

        deals_sorted = (getattr(state, "games_by_user", {}) or {}).get(int(uid), [])
        if not deals_sorted:
            logger.debug("[my_games] skip soft redraw: no cached deals for uid=%s", uid)
            return

        state.report_uid_hint = int(uid)

        bot = Bot.get_current()
        markup = _build_dashboard_kb(int(uid), deals_sorted)
        await bot.edit_message_reply_markup(chat_id=int(uid), message_id=int(header_mid), reply_markup=markup)
    except Exception as e:
        logger.warning("[my_games] soft redraw failed for uid=%s: %s", uid, e)


# ════════════════════════════════════════════════════════════════════
# [4] ВЫБОРКА ИГР
# ════════════════════════════════════════════════════════════════════
from typing import Any, Dict, List, Optional, Set
import logging

from core.config import settings
from core.state import state
from core.utils import assigned_role_from_state  # SSOT: присутствует для совместимости

logger = logging.getLogger(__name__)


def _success_status_id() -> str:
    """
    Безопасно возвращает ID статуса «Завершение сделки» из настроек.
    Поддерживаем несколько ключей для совместимости.
    """
    for key in ("SUCCESSFUL_STATUS_ID", "OK_STATUS_ID", "SUCCESS_STATUS_ID"):
        try:
            val = getattr(settings, key)  # type: ignore[attr-defined]
            if val:
                return str(val)
        except Exception:
            continue
    return ""


def _prelim_status_id() -> str:
    """
    Безопасно возвращает ID статуса «Предварительная заявка» из настроек.
    """
    for key in ("PRELIM_STATUS_ID", "PRELIMINARY_STATUS_ID"):
        try:
            val = getattr(settings, key)  # type: ignore[attr-defined]
            if val:
                return str(val)
        except Exception:
            continue
    return ""


def _wanted_status(deal: Dict) -> bool:
    """
    Игра попадает в «Мои игры», если её статус один из допустимых:
    • «Бронь» (settings.BRON_STATUS_ID),
    • «Предварительная заявка» (settings.PRELIM_STATUS_ID или по названию),
    • «Завершение сделки» (settings.SUCCESSFUL_STATUS_ID или по названию).

    Если статус не распознан — фолбэк «Бронь».
    """
    try:
        sid_any = _safe_status_id(deal)  # определена выше в файле
    except Exception:
        sid_any = None

    bron_id = str(getattr(settings, "BRON_STATUS_ID", ""))
    prelim_id = _prelim_status_id()
    success_id = _success_status_id()

    sid = str(sid_any or bron_id)
    name = str(deal.get("status_name") or deal.get("status") or "").strip().lower()

    prelim_names = {"предварительная заявка", "предварительно", "предварит.", "предварит", "prelim"}
    success_names = {"завершение сделки", "успешно реализовано", "успешно", "завершена"}

    if sid == bron_id:
        return True
    if prelim_id and sid == prelim_id:
        return True
    if success_id and sid == success_id:
        return True
    if name in prelim_names:
        return True
    if name in success_names:
        return True

    # фолбэк: неизвестный → считаем «Бронь»
    return sid == bron_id


def _assigned_deal_ids_from_locked(uid: int) -> Set[int]:
    """
    Собираем id всех сделок, где пользователь записан в ЗАФИКСИРОВАННОМ составе
    (слоты lead*/assistant*/admin/trainee). Источник — state.locked_distribution.

    Поддерживаем строки и коллекции (list/tuple) значений слотов.
    """
    out: Set[int] = set()
    locked = (getattr(state, "locked_distribution", {}) or {})
    for did_key, dist in locked.items():
        if not isinstance(dist, dict):
            continue
        try:
            did = int(did_key)
        except Exception:
            continue
        for k, v in dist.items():
            if not isinstance(k, str):
                continue
            if k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}:
                if isinstance(v, str):
                    if _label_belongs_to_uid(v, uid):  # определена выше
                        out.add(did)
                        break
                elif isinstance(v, (list, tuple)):
                    if any(isinstance(lbl, str) and _label_belongs_to_uid(lbl, uid) for lbl in v):
                        out.add(did)
                        break
    return out


def _assigned_role_via_locked(uid: int, deal_id: int) -> Optional[str]:
    """
    Роль пользователя строго по ЗАФИКСИРОВАННОМУ составу:
    используем объединение источников locked_distribution ∪ finished_locked_distribution
    (при наличии «активного» распределения оно имеет приоритет).

    Возвращает: 'main' | 'assist' | 'admin' | 'trainee' | None
    """
    # Вызов SSOT-хелпера сохраняем для совместимости/телеметрии, но решение принимаем по locked.
    try:
        _ = assigned_role_from_state(uid, deal_id)  # не используем результат, полагаемся на locked
    except Exception:
        pass

    locked_all = (getattr(state, "locked_distribution", {}) or {})
    finished_all = (getattr(state, "finished_locked_distribution", {}) or {})

    # поддерживаем int/str ключи и приоритет активного распределения
    dist: Optional[Dict[str, Any]] = None
    raw = locked_all.get(deal_id) or locked_all.get(str(deal_id))
    if isinstance(raw, dict):
        dist = raw
    else:
        raw_f = finished_all.get(deal_id) or finished_all.get(str(deal_id))
        if isinstance(raw_f, dict):
            dist = raw_f

    if not isinstance(dist, dict):
        return None

    def _belongs(val: Any) -> bool:
        if isinstance(val, str):
            return _label_belongs_to_uid(val, uid)
        if isinstance(val, (list, tuple)):
            return any(isinstance(lbl, str) and _label_belongs_to_uid(lbl, uid) for lbl in val)
        return False

    for k, v in dist.items():
        if not isinstance(k, str):
            continue
        if k.startswith("lead") and _belongs(v):
            return "main"
        if k.startswith("assistant") and _belongs(v):
            return "assist"
        if k == "admin" and _belongs(v):
            return "admin"
        if k == "trainee" and _belongs(v):
            return "trainee"
    return None


def _augment_with_locked(uid: int, all_deals: List[Dict]) -> List[Dict]:
    """
    Дополняем CRM-список сделками из утверждённого состава,
    чтобы игры были видны даже при пустом ответе CRM.

    База назначений — объединение источников:
      locked_distribution ∪ finished_locked_distribution

    Источники карточки (по приоритету):
      1) уже в all_deals (CRM),
      2) state.current_poll_deals,
      3) state.games_by_user,
      4) state.poll_details / state.deal_titles (минимальный фолбэк).

    Жёсткая дедупликация по id (by_id).
    """
    # 1) active
    want_ids_active: Set[int] = _assigned_deal_ids_from_locked(uid)

    # 2) finished_* — сканируем так же, как в _assigned_deal_ids_from_locked
    want_ids_finished: Set[int] = set()
    raw_finished = (getattr(state, "finished_locked_distribution", {}) or {})
    if isinstance(raw_finished, dict):
        for did_key, dist in raw_finished.items():
            if not isinstance(dist, dict):
                continue
            try:
                did = int(did_key)
            except Exception:
                continue
            for k, v in dist.items():
                if not isinstance(k, str):
                    continue
                if k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}:
                    if isinstance(v, str):
                        if _label_belongs_to_uid(v, uid):
                            want_ids_finished.add(did)
                            break
                    elif isinstance(v, (list, tuple)):
                        if any(isinstance(lbl, str) and _label_belongs_to_uid(lbl, uid) for lbl in v):
                            want_ids_finished.add(did)
                            break

    want_ids: Set[int] = want_ids_active | want_ids_finished
    logger.debug("[my_games] augment_with_locked: using union(active+finished), want_ids=%s", sorted(want_ids))

    by_id: Dict[int, Dict] = {}
    out: List[Dict] = []
    for d in all_deals or []:
        try:
            did = int(d.get("id") or 0)
        except Exception:
            continue
        if did and did not in by_id:
            by_id[did] = d
            out.append(d)

    if not want_ids:
        return list(out)

    def _find_snap_in_poll(did: int) -> Optional[Dict]:
        try:
            pool = (getattr(state, "current_poll_deals", []) or [])
            return next((x for x in pool if int(x.get("id") or 0) == did), None)
        except Exception:
            return None

    def _find_snap_in_users(did: int) -> Optional[Dict]:
        try:
            for deals in (getattr(state, "games_by_user", {}) or {}).values():
                x = next((t for t in (deals or []) if int(t.get("id") or 0) == did), None)
                if x:
                    return x
        except Exception:
            return None
        return None

    for did in sorted(want_ids):
        if did in by_id:
            continue

        snap: Optional[Dict] = _find_snap_in_poll(did) or _find_snap_in_users(did)

        # 4) минимальный фолбэк по poll_details / deal_titles
        if not snap:
            details = (getattr(state, "poll_details", {}) or {}).get(did) or {}
            title = (
                details.get("title")
                or (getattr(state, "deal_titles", {}) or {}).get(did)
                or f"Сделка #{did}"
            )
            snap = {
                "id": did,
                "name": title,
                "event_datetime": details.get("event_datetime"),
                "event_time": details.get("event_time") or "—",
                "address": details.get("address") or details.get("location") or "—",
                "package": details.get("package") or "—",
                "players": details.get("players") or "—",
                "tags": details.get("tags") or [],
                "comment": details.get("comment") or "",
                # статус обязателен — по умолчанию «Бронь», чтобы пройти фильтр
                "status_id": str(getattr(settings, "BRON_STATUS_ID", "")),
            }

        try:
            did2 = int(snap.get("id") or 0)  # type: ignore[arg-type]
        except Exception:
            did2 = 0
        if did2 and did2 not in by_id:
            by_id[did2] = snap  # type: ignore[assignment]
            out.append(dict(snap))

    return out


def _visible_deals_for_user(uid: int, all_deals: List[Dict]) -> List[Dict]:
    """
    Итоговая выборка для «Мои игры»:
      • статус: «Бронь» / «Предварительная заявка» / «Завершение сделки»;
      • пользователь назначен И УТВЕРЖДЁН (locked_distribution ∪ finished_locked_distribution / SSOT);
      • + дополнение из локального кэша, если CRM вернул пусто.

    Дубли исключаем (by_id/seen). Сортировка по дате мероприятия.
    """
    all_deals = _augment_with_locked(uid, list(all_deals or []))

    out: List[Dict] = []
    seen: Set[int] = set()

    def _to_epoch(dt_obj):
        try:
            if dt_obj is None:
                return float("inf")
            if getattr(dt_obj, "tzinfo", None):
                return dt_obj.timestamp()
            # локализуем «наивное» время в МСК для стабильной сортировки
            return dt_obj.replace(tzinfo=MSK_TZ).timestamp()
        except Exception:
            return float("inf")

    def _key(d: Dict):
        dt = _safe_event_dt(d)
        return (dt is None, _to_epoch(dt))

    for d in sorted(all_deals, key=_key):
        try:
            did = int(d.get("id") or 0)
        except Exception:
            continue
        if not did or did in seen:
            continue
        # не показываем удалённые сделки
        if bool(d.get("is_deleted")):
            continue
        if not _wanted_status(d):
            continue

        # Единственный валидный критерий — утверждение через locked_distribution ∪ finished_locked_distribution
        if not _assigned_role_via_locked(uid, did):
            continue

        out.append(d)
        seen.add(did)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[my_games] visible_deals uid=%s -> %s", uid, [int(x.get("id") or 0) for x in out])

    return out


# История изменений [4]:
# • 2025-08-31 — учитываем union(active+finished) для want_ids и определения роли; добавлен DEBUG-лог
#                '[my_games] augment_with_locked: using union(active+finished), want_ids=...'.
# • 2025-08-31 — не показываем удалённые сделки (is_deleted); выровнено под SSOT/фиксы Pylance.
# • 2025-08-30 — добавлен статус «Предварительная заявка» (ID + текстовые синонимы);
#                выборка жёстко ограничена только locked_distribution; фиксы Pylance.
# • 2025-08-29 — безопасная сортировка по timestamp (naive/aware); распознавание success-name.
# • 2025-08-24 — дедупликация и коллекции в слотах; отладочный лог augment_with_locked.
# • 2025-08-19 — учёт слотов без «|uid» + фолбэк по ярлыку.



# ════════════════════════════════════════════════════════════════════
# [4.1] Backward compatibility (for handlers.profile import)
# ════════════════════════════════════════════════════════════════════
def _my_games(uid: int, deals: List[Dict]) -> List[Dict]:
    """Совместимость со старыми модулями (handlers.profile)."""
    return _visible_deals_for_user(uid, deals)





# ════════════════════════════════════════════════════════════════════
# ███ [5] DASHBOARD / DETAILS — липкий дашборд + пылесос как в отчёте
# ════════════════════════════════════════════════════════════════════
# Версия 5.10.1 · 2025-09-06 (hotfix-3)
# Задачи/поведение:
# 1) При входе в «Мои игры» — репорт-дашборд опроса полностью исчезает (не «уезжает вверх»):
#    • агрессивная зачистка реестров репорта/деталей перед отрисовкой;
#    • подавление keep leader report (если поддерживается ядром) во всех вакуумах из «Моих игр».
# 2) «Тихие» замены кнопок:
#    • после подтверждения — «✅ Подтверждено», без удаления дашборда;
#    • после SUCCESS — «🔁 Замена» в дашборде и «🙋 Попросить замену» в деталях;
#    • если sticky внезапно пропал — тихо пересоздаём из кэша и продолжаем.
#
# Минимальные правки по сравнению с 5.10.0:
# • Глобально и локально гарантируем включение sticky в keep (патч keep_for_vacuum + локальный keep_for_vacuum).
# • Вакууум из «Моих игр» всегда выполняется с подавлением leader report (безоткатно на короткий интервал).
# • _vacuum_poll_details_blocks агрессивно чистит все известные/эвристические хранилища репорта (включая leader).
# • Добавлено безопасное определение _cu (core.utils) для использования в подавлении и в эвристике.
import logging
from contextlib import suppress
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Callable, cast, Set, Tuple
from datetime import datetime

from aiogram import Bot, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.state import state
from core.utils import truncate, vacuum_private as ssot_vacuum_private, keep_for_vacuum as ssot_keep_for_vacuum

# локальная таймзона для сортировки дат (без зависимости от core.utils)
from pytz import timezone as _tz
try:
    from core.config import settings as _cfg_tz  # type: ignore
    _TZ_NAME: str = (getattr(_cfg_tz, "TIMEZONE", None) or "Europe/Moscow")
except Exception:
    _TZ_NAME = "Europe/Moscow"
LOCAL_TZ = _tz(_TZ_NAME)

logger = logging.getLogger(__name__)

# ── безопасное обращение к core.utils (для подавления leader report и эвристик) ──
_cu = None
with suppress(Exception):
    import core.utils as _cu  # type: ignore

# ── Глобальный патч SSOT keep_for_vacuum: стараемся хранить наш sticky везде ──
try:
    if _cu and not getattr(state, "_mygames_keep_patch_installed", False):
        _orig_keep = _cu.keep_for_vacuum  # type: ignore[attr-defined]

        def _keep_wrapper(uid: int, *extra: int):
            keep_extra: List[int] = list(extra)
            sticky = None
            with suppress(Exception):
                sticky = get_my_games_dashboard(int(uid))
            if isinstance(sticky, int) and sticky > 0 and sticky not in keep_extra:
                keep_extra.append(int(sticky))
            try:
                res = _orig_keep(int(uid), *keep_extra)
                sig = "(uid, *extra)"
            except TypeError:
                res = _orig_keep(int(uid))  # type: ignore[misc]
                sig = "(uid)"
                if isinstance(sticky, int) and sticky > 0 and sticky not in res:
                    res.append(int(sticky))
            if isinstance(sticky, int) and sticky > 0 and sticky not in res:
                res.append(int(sticky))
            logger.debug("[my_games.keep_patch] uid=%s sig=%s extra=%s -> keep=%s", uid, sig, keep_extra, res)
            return res

        _cu.keep_for_vacuum = _keep_wrapper  # type: ignore[assignment]
        state._mygames_keep_patch_installed = True  # type: ignore[attr-defined]
        logger.info("[my_games.keep_patch] core.utils.keep_for_vacuum patched globally")
except Exception as e:
    logger.warning("[my_games.keep_patch] cannot patch core.utils.keep_for_vacuum: %s", e)

# ── sticky-реестр дашборда ──────────────────────────────────────────
if not hasattr(state, "my_games_dashboard"):
    # uid -> message_id
    state.my_games_dashboard: Dict[int, int] = {}  # type: ignore[attr-defined]

def get_my_games_dashboard(uid: int) -> Optional[int]:
    try:
        mid = (getattr(state, "my_games_dashboard", {}) or {}).get(int(uid))
        return int(mid) if mid else None
    except Exception:
        return None

def set_my_games_dashboard(uid: int, message_id: int) -> None:
    (getattr(state, "my_games_dashboard", {}) or {}).update({int(uid): int(message_id)})

def keep_for_vacuum(uid: int, *extra_msg_ids: int) -> List[int]:
    """Локальный keep: sticky «Мои игры» + SSOT + явные доп. id."""
    keep: List[int] = []

    sticky = get_my_games_dashboard(int(uid))
    if isinstance(sticky, int) and sticky > 0:
        keep.append(sticky)

    with suppress(Exception):
        for mid in ssot_keep_for_vacuum(int(uid), *keep):
            if isinstance(mid, int) and mid > 0 and mid not in keep:
                keep.append(mid)

    for mid in extra_msg_ids:
        try:
            m2 = int(mid)
        except Exception:
            continue
        if m2 > 0 and m2 not in keep:
            keep.append(m2)

    logger.debug("[my_games.vacuum] keep_for_vacuum(uid=%s) -> keep_ids=%s", uid, keep)
    return keep

# ── локальный флаг подтверждения для «тихой» перекраски ─────────────
if not hasattr(state, "mygames_local_confirm"):
    # ключ: (uid, deal_id) -> bool
    state.mygames_local_confirm: Dict[Tuple[int, int], bool] = {}  # type: ignore[attr-defined]

def _set_locally_confirmed(uid: int, deal_id: int, flag: bool = True) -> None:
    try:
        state.mygames_local_confirm[(int(uid), int(deal_id))] = bool(flag)
        logger.debug("[my_games.local] set confirmed uid=%s deal=%s -> %s", uid, deal_id, flag)
    except Exception as e:
        logger.debug("[my_games.local] set confirmed failed: %s", e)

def _mg_local_confirmed(uid: int, deal_id: int) -> bool:
    try:
        return bool((getattr(state, "mygames_local_confirm", {}) or {}).get((int(uid), int(deal_id))))
    except Exception:
        return False

# ── внешние хелперы из других блоков (типизированные заглушки) ─────
if TYPE_CHECKING:
    async def _vacuum_poll_details_blocks(uid: int) -> None: ...
    def _safe_event_dt(deal: Dict[str, Any]) -> Optional[datetime]: ...
    def _safe_title(deal: Dict[str, Any]) -> str: ...
    def _safe_status_id(deal: Dict[str, Any]) -> str: ...
    def make_my_games_confirm_btn_for_row(deal: Dict[str, Any], uid: int) -> Optional[InlineKeyboardButton]: ...
    def make_my_games_swap_btn_for_row(deal: Dict[str, Any], uid: int) -> Optional[InlineKeyboardButton]: ...
else:
    _vacuum_poll_details_blocks = cast(Callable[[int], Any], globals().get("_vacuum_poll_details_blocks"))  # type: ignore
    _safe_event_dt = cast(Callable[[Dict[str, Any]], Optional[datetime]], globals().get("_safe_event_dt", lambda *_: None))
    _safe_title = cast(Callable[[Dict[str, Any]], str], globals().get("_safe_title", lambda d: str(d.get("name") or "")))
    _safe_status_id = cast(Callable[[Dict[str, Any]], str], globals().get("_safe_status_id", lambda d: str(d.get("status_id") or "")))
    make_my_games_confirm_btn_for_row = cast(Callable[[Dict[str, Any], int], Optional[InlineKeyboardButton]], globals().get("make_my_games_confirm_btn_for_row"))  # type: ignore
    make_my_games_swap_btn_for_row   = cast(Callable[[Dict[str, Any], int], Optional[InlineKeyboardButton]], globals().get("make_my_games_swap_btn_for_row"))    # type: ignore

# ── подавление keep leader report в ядре (если поддерживается) ──────
class _SuppressLeaderReport:
    def __init__(self, uid: int):
        self.uid = int(uid)

    async def __aenter__(self):
        # пытаемся включить все известные рубильники
        if _cu:
            with suppress(Exception):
                setattr(_cu, "SUPPRESS_KEEP_LEADER_REPORT", True)   # глобально
            with suppress(Exception):
                s = getattr(_cu, "suppress_keep_leader_report_uids", None)
                if isinstance(s, set):
                    s.add(self.uid)
        return self

    async def __aexit__(self, *exc):
        # откат не делаем — ядро обычно снимает флаги само, это снижает гонки
        return False

# ── общий пылесос ЛС (сохраняет sticky «Мои игры») ──────────────────
async def _vacuum_safe(uid: int, keep: Optional[List[Any]] = None, *, ignore_sticky: bool = False) -> None:
    """
    Пылесос ЛС для «Моих игр».
    • Сохраняет sticky-дашборд (если ignore_sticky=False).
    • Чистит хвосты деталей/репорта перед/после вакуума.
    • Использует core.utils.vacuum_private с поддержкой suppress-флагов.
    """
    from aiogram import types as _types
    logger.debug("[my_games.vacuum] start: uid=%s ignore_sticky=%s raw_keep=%s", uid, ignore_sticky, keep)

    # предварительно уберём детали/репорт-хвосты
    with suppress(Exception):
        res = _vacuum_poll_details_blocks(int(uid))
        if hasattr(res, "__await__"):
            await res
        logger.debug("[my_games.vacuum] pre-clean poll_details/report done for uid=%s", uid)

    keep_ids: List[int] = []
    for k in keep or []:
        with suppress(Exception):
            if isinstance(k, _types.Message):
                keep_ids.append(int(k.message_id))
            elif isinstance(k, int):
                keep_ids.append(int(k))

    if not ignore_sticky:
        for mid in keep_for_vacuum(int(uid)):
            if mid not in keep_ids and isinstance(mid, int) and mid > 0:
                keep_ids.append(mid)

    bot = Bot.get_current()

    async with _SuppressLeaderReport(int(uid)):
        with suppress(Exception):
            logger.info("[my_games.vacuum] executing vacuum for uid=%s; keep_ids=%s", uid, keep_ids)
            try:
                await ssot_vacuum_private(bot, int(uid), keep=keep_ids, suppress=True, suppress_leader_report=True)  # type: ignore[call-arg]
                logger.debug("[my_games.vacuum] ssot_vacuum_private OK (bot, uid, keep, suppress=*)")
            except TypeError:
                try:
                    await ssot_vacuum_private(int(uid), keep=keep_ids, suppress=True)  # type: ignore[call-arg]
                    logger.debug("[my_games.vacuum] ssot_vacuum_private OK (uid, keep, suppress=*)")
                except TypeError:
                    try:
                        await ssot_vacuum_private(int(uid), keep=keep_ids)
                        logger.debug("[my_games.vacuum] ssot_vacuum_private OK (uid, keep)")
                    except TypeError:
                        await ssot_vacuum_private(int(uid))
                        logger.debug("[my_games.vacuum] ssot_vacuum_private OK (uid,)")

            with suppress(Exception):
                res2 = _vacuum_poll_details_blocks(int(uid))
                if hasattr(res2, "__await__"):
                    await res2
                logger.debug("[my_games.vacuum] post-clean poll_details/report done for uid=%s", uid)
            return

    # фолбэк — старый пылесос
    with suppress(Exception):
        from core.utils import delete_previous_private_messages as _old  # type: ignore
        logger.info("[my_games.vacuum] fallback to legacy delete_previous_private_messages for uid=%s", uid)
        try:
            await _old(bot, int(uid), keep=keep_ids)
            logger.debug("[my_games.vacuum] legacy vacuum OK (bot, uid, keep)")
        except TypeError:
            try:
                await _old(int(uid), keep=keep_ids)
                logger.debug("[my_games.vacuum] legacy vacuum OK (uid, keep)")
            except TypeError:
                await _old(int(uid))
                logger.debug("[my_games.vacuum] legacy vacuum OK (uid,)")

        with suppress(Exception):
            res3 = _vacuum_poll_details_blocks(int(uid))
            if hasattr(res3, "__await__"):
                await res3
            logger.debug("[my_games.vacuum] post-clean poll_details/report done (legacy) for uid=%s", uid)

# ── построение клавиатуры дашборда (v2) ─────────────────────────────
def _build_dashboard_kb_v2(uid: int, deals_sorted: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Кнопки «✅ Подтвердить»/«🔁 Замена» строятся локально по SSOT:
    • «✅ Подтверждено» — до SUCCESS (по тегу/локальному флагу);
    • «🔁 Замена» — только на SUCCESS.
    """
    from core.config import settings as _cfg
    BRON_ID: str   = str(getattr(_cfg, "BRON_STATUS_ID", ""))
    OK_ID: str     = str(getattr(_cfg, "SUCCESSFUL_STATUS_ID", ""))
    PRELIM_ID: str = str(getattr(_cfg, "PRELIM_STATUS_ID", getattr(_cfg, "PRELIMINARY_STATUS_ID", "")) or "")

    _has_confirmation_tag = cast(Callable[[Dict[str, Any], int], bool], globals().get("_has_confirmation_tag"))  # type: ignore
    _assigned_role_from_state = cast(Callable[[int, int], Optional[str]], globals().get("_assigned_role_from_state"))  # type: ignore
    _is_locally_confirmed_for_redraw = cast(Callable[[int, int], bool], globals().get("_is_locally_confirmed_for_redraw"))  # type: ignore
    _report_available_for = cast(Callable[[int], bool], globals().get("_report_available_for"))  # type: ignore

    kb = InlineKeyboardBuilder()

    for d in deals_sorted:
        did = int(d.get("id") or 0)
        title = truncate(_safe_title(d), 28)
        dt = _safe_event_dt(d)
        date = dt.strftime("%d.%m") if dt else "??.??"

        sid = _safe_status_id(d)
        name = str(d.get("status_name") or d.get("status") or "").strip().lower()
        prelim_names = {"предварительная заявка", "предварительно", "предварит."}
        if (PRELIM_ID and sid == PRELIM_ID) or (name in prelim_names):
            status = "Предвар."
        else:
            status = "Бронь" if sid == BRON_ID else "Заверш."

        kb.button(
            text=f"ℹ️ {title} · {date} · {status}",
            callback_data=f"{globals().get('DETAILS_PREFIX','mygame_details_')}{did}",
        )

        # состояние подтверждения: локальный флаг → CRM-теги → локальная отметка [3.4]
        confirmed = _mg_local_confirmed(uid, did)
        if not confirmed and callable(_has_confirmation_tag):
            with suppress(Exception):
                confirmed = bool(_has_confirmation_tag(d, uid))
        if not confirmed and callable(_is_locally_confirmed_for_redraw):
            with suppress(Exception):
                confirmed = bool(_is_locally_confirmed_for_redraw(did, uid))

        # роль из зафиксированного распределения
        role: Optional[str] = None
        if callable(_assigned_role_from_state):
            with suppress(Exception):
                role = _assigned_role_from_state(uid, did)

        # строка действия
        if sid == OK_ID:
            kb.row(InlineKeyboardButton(
                text="🔁 Замена",
                callback_data=f"{globals().get('SWAP_PREFIX','mygame_swap_')}{did}"
            ))
        elif confirmed:
            kb.row(InlineKeyboardButton(text="✅ Подтверждено", callback_data="mygame_noop"))
        elif role in {"main", "assist", "admin"} and (sid == BRON_ID or status == "Предвар."):
            kb.row(InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"{globals().get('CONFIRM_PREFIX','confirm_role_')}{did}_{role}",
            ))

        # «Отчёт» — отдельной строкой при наступлении доступности
        if callable(_report_available_for):
            with suppress(Exception):
                if _report_available_for(did):
                    kb.row(InlineKeyboardButton(text="📝 Написать отчёт", callback_data=f"{globals().get('REPORT_PREFIX','mygame_report_')}{did}"))

    kb.adjust(1)
    return kb.as_markup()

# ── «тихая» перекраска только кнопок текущего дашборда ──────────────
async def update_my_games_buttons_only(uid: int, markup: InlineKeyboardMarkup | None) -> Optional[int]:
    mid = get_my_games_dashboard(int(uid))
    if not isinstance(mid, int) or mid <= 0:
        logger.debug("[my_games] buttons-only: no sticky slot for uid=%s", uid)
        return None
    with suppress(Exception):
        await Bot.get_current().edit_message_reply_markup(chat_id=int(uid), message_id=int(mid), reply_markup=markup)
        await _vacuum_safe(int(uid), keep=[mid])  # мягкий подмет: сохранить sticky, убрать хвосты/репорт
        return int(mid)
    return None

# ── основной вывод/обновление дашборда ──────────────────────────────
async def _send_dashboard(uid: int, deals: List[Dict[str, Any]]) -> None:
    """
    Отрисовывает (или обновляет) список игр.
    Гарантии:
    • Перед отрисовкой агрессивно стираем дашборд отчёта опроса.
    • При редактировании — не удаляем sticky; при отсутствии — создаём новый.
    """
    bot = Bot.get_current()
    logger.info("[my_games] send_dashboard: uid=%s deals_in=%s", uid, len(deals or []))

    # (1) Сначала подчистим репорт-дашборд/детали, чтобы он не «уехал вверх»
    with suppress(Exception):
        res = _vacuum_poll_details_blocks(int(uid))
        if hasattr(res, "__await__"):
            await res

    # (2) Сортировка карточек по дате/времени
    def _key(d: Dict[str, Any]) -> tuple:
        dt = _safe_event_dt(d)
        try:
            if dt is None:
                ts = float("inf")
            elif getattr(dt, "tzinfo", None):
                ts = dt.timestamp()
            else:
                ts = LOCAL_TZ.localize(dt).timestamp()
        except Exception:
            ts = float("inf")
        return (dt is None, ts)

    deals_sorted = sorted(list(deals or []), key=_key)
    logger.debug("[my_games] send_dashboard: sorted_count=%s", len(deals_sorted))

    # Клавиатура
    build_kb = globals().get("_build_dashboard_kb")
    if callable(build_kb):
        logger.debug("[my_games] send_dashboard: keyboard via _build_dashboard_kb")
        markup = build_kb(int(uid), deals_sorted)  # type: ignore[misc]
    else:
        logger.debug("[my_games] send_dashboard: keyboard via _build_dashboard_kb_v2")
        markup = _build_dashboard_kb_v2(int(uid), deals_sorted)

    text = "🎲 *Мои игры:*"

    # (3) Попытка тихого обновления существующего sticky
    mid = get_my_games_dashboard(int(uid))
    logger.debug("[my_games] send_dashboard: current_sticky=%s", mid)
    if isinstance(mid, int) and mid > 0:
        edited_ok = False
        with suppress(Exception):
            msg = await bot.edit_message_text(
                chat_id=int(uid),
                message_id=int(mid),
                text=text,
                parse_mode="Markdown",
                reply_markup=markup,
            )
            mid = int(getattr(msg, "message_id", mid))
            set_my_games_dashboard(int(uid), int(mid))
            edited_ok = True
        if edited_ok:
            await _vacuum_safe(int(uid), keep=[mid])  # мягкий вакуум
            (getattr(state, "games_by_user", {}) or {}).setdefault(int(uid), [])
            state.games_by_user[int(uid)] = deals_sorted
            (getattr(state, "last_user_messages", {}) or {}).setdefault(int(uid), [])
            state.last_user_messages[int(uid)] = []
            logger.info("[my_games] sticky updated: uid=%s mid=%s", uid, mid)
            return

    # (4) Sticky нет — создаём
    sent = await bot.send_message(int(uid), text, parse_mode="Markdown", reply_markup=markup)
    mid = int(sent.message_id)
    set_my_games_dashboard(int(uid), int(mid))
    logger.info("[my_games] sticky created: uid=%s mid=%s", uid, mid)

    # (5) После создания — подметём, сохраняя sticky
    await _vacuum_safe(int(uid), keep=[mid])

    # Кэши для быстрого редрава/деталей
    (getattr(state, "games_by_user", {}) or {}).setdefault(int(uid), [])
    state.games_by_user[int(uid)] = deals_sorted
    (getattr(state, "last_user_messages", {}) or {}).setdefault(int(uid), [])
    state.last_user_messages[int(uid)] = [sent]

# ── детали (fallback, без накопления хвостов) ───────────────────────
async def _send_details(uid: int, deal: Dict[str, Any]) -> None:
    """
    Детали «Моих игр». Основной рендер — через show_my_game_details(fake_cb), если доступен.
    """
    bot = Bot.get_current()
    deal_id = int(deal.get("id") or 0)

    # основной путь — переиспользуем публичный рендер деталей
    with suppress(Exception):
        fake_cb = types.CallbackQuery(
            id="0",
            from_user=types.User(id=int(uid), is_bot=False, first_name=""),
            chat_instance="",
            message=types.Message(
                message_id=get_my_games_dashboard(int(uid)) or 0,
                date=datetime.now(),
                chat=types.Chat(id=int(uid), type="private"),
            ),
            data=f"{globals().get('DETAILS_PREFIX','mygame_details_')}{deal_id}",
        )
        res = globals().get("show_my_game_details", None)
        if callable(res):
            coro = res(fake_cb)  # type: ignore[misc]
            if hasattr(coro, "__await__"):
                await coro
            return

    # фолбэк — минимум информации
    dt = _safe_event_dt(deal)
    date_s = dt.strftime("%d.%m.%Y") if dt else "—"
    time_s = str(deal.get("event_time") or "—")
    status_name = str(deal.get("status_name") or "—")
    text = f"ℹ️ {truncate(_safe_title(deal), 40)}\n📅 {date_s} · 🕒 {time_s}\n📌 Статус: {status_name}"

    # SUCCESS => «Попросить замену»
    from core.config import settings as _cfg
    OK_ID: str = str(getattr(_cfg, "SUCCESSFUL_STATUS_ID", ""))
    sid = _safe_status_id(deal)
    kb = InlineKeyboardBuilder()
    if OK_ID and sid == OK_ID:
        kb.button(text="🙋 Попросить замену", callback_data=f"{globals().get('SWAP_PREFIX','mygame_swap_')}{deal_id}")
        kb.row(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="mygames_back"))
    else:
        kb.button(text="⬅️ Назад к списку", callback_data="mygames_back")

    await bot.send_message(int(uid), text, reply_markup=kb.as_markup())



# [5.2] CROSS-MODULE VACUUM — удаление и обнуление реестров poll/report
from contextlib import suppress
from typing import Any, Dict, Set

async def _vacuum_poll_details_blocks(uid: int) -> None:
    """
    Жёсткая очистка «деталей/дашборда отчёта опроса» у пользователя:
      • если есть публичный API в handlers.poll_details — используем его;
      • иначе — best-effort по state и core.utils: собираем известные/эвристические
        реестры, удаляем связанные с пользователем сообщения, чистим записи, в т.ч.
        «leader report».
    Никогда не пробрасывает исключения наружу.
    """
    from aiogram import Bot
    bot = Bot.get_current()
    u = int(uid)

    # 0) Официальный API (если есть)
    with suppress(Exception):
        from handlers.poll_details import forget_all_details_for_user  # type: ignore
        if callable(forget_all_details_for_user):
            await forget_all_details_for_user(u, bot=bot)  # type: ignore[arg-type]
            logger.debug("[my_games.vacuum] poll_details: used official API for uid=%s", u)
            # продолжаем best-effort, чтобы убрать leader-report, если API его не трогает

    # ── утилиты ──────────────────────────────────────────────────────
    def _as_int(v: Any) -> Optional[int]:
        try:
            if isinstance(v, int):
                return int(v)
            if isinstance(v, str) and v.isdigit():
                return int(v)
        except Exception:
            pass
        return None

    def _collect_msg_ids(obj: Any, acc: Set[int]) -> None:
        if obj is None:
            return
        if isinstance(obj, int):
            acc.add(int(obj)); return
        if isinstance(obj, str):
            x = _as_int(obj)
            if x is not None:
                acc.add(x)
            return
        if isinstance(obj, (list, tuple, set)):
            for x in obj:
                _collect_msg_ids(x, acc)
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _collect_msg_ids(v, acc)
            return

    def _collect_for_uid(obj: Any, user_key: int | str, acc: Set[int]) -> None:
        if obj is None:
            return
        if isinstance(obj, dict):
            node = obj.get(user_key) or obj.get(str(user_key))
            if node is not None:
                _collect_msg_ids(node, acc)
            else:
                for v in obj.values():
                    _collect_for_uid(v, user_key, acc)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                _collect_for_uid(v, user_key, acc)

    def _purge_uid_entries(obj: Any, user_key: int | str) -> None:
        if isinstance(obj, dict):
            obj.pop(user_key, None)
            obj.pop(str(user_key), None)
            for v in list(obj.values()):
                _purge_uid_entries(v, user_key)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                _purge_uid_entries(v, user_key)

    # ── 1) Известные и эвристические «реестры» в state ───────────────
    candidate_names = [
        # poll details / report details
        "poll_details_blocks", "poll_detail_blocks",
        "poll_details_index",  "poll_detail_index",
        "pd_blocks", "pd_index",
        "report_details_blocks", "report_detail_blocks",
        "report_details_index",  "report_detail_index",
        "report_registry", "report_blocks_map", "report_msgs", "report_messages",
        "report_header", "report_rows", "report_footer",
        "detail_blocks", "details_blocks",
        "detail_index",  "details_index",
        # «дашборды» отчёта
        "report_dashboard", "report_sticky", "poll_report_dashboard", "polls_report_sticky",
        # возможные лидеры/сообщения лид-репорта
        "leader_report", "leader_report_mid", "leader_report_message", "leader_report_messages",
        "leader_report_by_uid", "leader_reports", "report_leader_dashboard", "report_leader_sticky",
        "current_leader_report", "current_report_message", "notify_report_message",
    ]
    registries: Dict[str, Any] = {}
    for name in candidate_names:
        with suppress(Exception):
            if hasattr(state, name):
                registries[name] = getattr(state, name)

    with suppress(Exception):
        for name in dir(state):
            low = name.lower()
            if ("report" in low or "poll" in low) and any(
                key in low for key in ("detail", "dashboard", "block", "index", "msg", "message", "registry", "leader", "sticky")
            ):
                if name not in registries:
                    with suppress(Exception):
                        registries[name] = getattr(state, name)

    # ── 2) Похожие «реестры» в core.utils (часто там хранится leader report) ──
    registries_cu: Dict[str, Any] = {}
    if _cu:
        with suppress(Exception):
            for name in dir(_cu):
                low = name.lower()
                if ("report" in low or "poll" in low) and any(
                    key in low for key in ("leader", "dashboard", "sticky", "msg", "message")
                ):
                    with suppress(Exception):
                        registries_cu[name] = getattr(_cu, name)

    # ── 3) Собираем message_id и стираем ─────────────────────────────
    ids: Set[int] = set()

    for _, reg in list(registries.items()):
        with suppress(Exception):
            if isinstance(reg, dict):
                node = reg.get(u) or reg.get(str(u))
                if node is not None:
                    _collect_msg_ids(node, ids)
                else:
                    _collect_for_uid(reg, u, ids)
            else:
                _collect_msg_ids(reg, ids)

    for _, reg in list(registries_cu.items()):
        with suppress(Exception):
            if isinstance(reg, dict):
                node = reg.get(u) or reg.get(str(u))
                if node is not None:
                    _collect_msg_ids(node, ids)
                else:
                    _collect_for_uid(reg, u, ids)
            else:
                _collect_msg_ids(reg, ids)

    for mid in sorted(ids):
        with suppress(Exception):
            await bot.delete_message(chat_id=u, message_id=int(mid))

    # зачистить записи по uid
    for reg in list(registries.values()):
        with suppress(Exception):
            if isinstance(reg, dict):
                reg.pop(u, None); reg.pop(str(u), None)
                _purge_uid_entries(reg, u)
            elif isinstance(reg, list):
                for v in reg:
                    _purge_uid_entries(v, u)

    for reg in list(registries_cu.values()):
        with suppress(Exception):
            if isinstance(reg, dict):
                reg.pop(u, None); reg.pop(str(u), None)
                _purge_uid_entries(reg, u)

    # зачистка last_user_messages от удалённых id
    with suppress(Exception):
        lum = (getattr(state, "last_user_messages", {}) or {}).get(u) or []
        if isinstance(lum, list) and lum:
            new_lum = []
            ids_set = set(ids)
            for msg in lum:
                try:
                    mid = int(getattr(msg, "message_id", 0))
                except Exception:
                    mid = 0
                if mid and mid in ids_set:
                    continue
                new_lum.append(msg)
            (getattr(state, "last_user_messages", {}) or {}).__setitem__(u, new_lum)  # type: ignore[index]
    # тихо выходим
    return


# ════════════════════════════════════════════════════════════════════
# [6] PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def redraw_my_games(uid: int) -> None:
    """
    Перерисовывает дашборд «Мои игры».
    • Если уже в контексте 'my_games' и есть мягкий редрав — используем его.
    • Иначе — тянем сделки, ПЕРЕД отрисовкой стираем репорт-дашборд, далее рисуем sticky.
    """
    # быстрый путь
    try:
        ctx = (getattr(state, "ui_context", {}) or {}).get(int(uid))
        soft_redraw = globals().get("_soft_redraw_my_games")
        if ctx == "my_games" and callable(soft_redraw):
            await soft_redraw(uid)  # type: ignore[misc]
            return
    except Exception:
        pass

    # полный путь
    try:
        all_deals = await get_amocrm_deals()
    except Exception as e:
        logger.error("[my_games:redraw] get_amocrm_deals failed: %s", e)
        # даже при ошибке — подчистим репорт-дашборд, чтобы он не «липнул»
        with suppress(Exception):
            await _vacuum_poll_details_blocks(int(uid))
        await _vacuum_safe(uid)  # мягкая уборка
        await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        return

    deals = _visible_deals_for_user(uid, all_deals)
    if deals:
        await _send_dashboard(uid, deals)
    else:
        with suppress(Exception):
            await _vacuum_poll_details_blocks(int(uid))
        await _vacuum_safe(uid)
        await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")

# ────────────────────────────────────────────────────────────────────
# [6.x] QUIET PATCH: после подтверждения (handlers.confirmations вызывает)
async def mygames_after_confirm_ui_patch(uid: int, deal_id: int) -> bool:
    """
    После успешного подтверждения — тихо помечаем строку «✅ Подтверждено»
    и обновляем ТОЛЬКО клавиатуру sticky-дашборда. Если sticky внезапно удалён,
    пересоздаём дашборд из кэша и всё равно возвращаем ok.
    """
    with suppress(Exception):
        _set_locally_confirmed(int(uid), int(deal_id), True)

    deals_sorted = (getattr(state, "games_by_user", {}) or {}).get(int(uid)) or []
    if not deals_sorted:
        logger.debug("[my_games.patch] after_confirm: no cached deals for uid=%s", uid)
        return False

    mid = get_my_games_dashboard(int(uid))
    if not mid or mid <= 0:
        logger.debug("[my_games.patch] after_confirm: sticky missing, silently re-create")
        with suppress(Exception):
            await _send_dashboard(int(uid), deals_sorted)  # создаст sticky со «✅ Подтверждено»
        return True

    build_kb = globals().get("_build_dashboard_kb")
    if callable(build_kb):
        markup = build_kb(int(uid), deals_sorted)  # type: ignore[misc]
    else:
        markup = globals().get("_build_dashboard_kb_v2")(int(uid), deals_sorted)  # type: ignore[misc]

    mid2 = await globals().get("update_my_games_buttons_only")(int(uid), markup)  # type: ignore[misc]
    ok = bool(mid2)
    logger.info("[my_games.patch] after_confirm: uid=%s deal=%s mid=%s ok=%s", uid, deal_id, mid2, ok)
    return ok

# QUIET PATCH: после SUCCESS (вся команда подтвердила, сделка → SUCCESS)
async def mygames_after_success_ui_patch(uid: int, deal_id: int) -> bool:
    """
    Тихо переключает строку сделки в «🔁 Замена» после SUCCESS
    и снимает локальную отметку подтверждения (если была).
    """
    from core.config import settings as _cfg
    OK_ID: str = str(getattr(_cfg, "SUCCESSFUL_STATUS_ID", ""))

    deals_sorted: List[Dict[str, Any]] = (getattr(state, "games_by_user", {}) or {}).get(int(uid)) or []
    for d in deals_sorted:
        if int(d.get("id") or 0) == int(deal_id):
            d["status_id"] = OK_ID or d.get("status_id")
            d["status_name"] = "Завершена"
            break

    with suppress(Exception):
        (getattr(state, "mygames_local_confirm", {}) or {}).pop((int(uid), int(deal_id)), None)

    if not deals_sorted:
        return False

    mid = get_my_games_dashboard(int(uid))
    if not mid or mid <= 0:
        logger.debug("[my_games.patch] after_success: sticky missing, silently re-create")
        with suppress(Exception):
            await _send_dashboard(int(uid), deals_sorted)  # создаст sticky с «🔁 Замена»
        return True

    build_kb = globals().get("_build_dashboard_kb")
    if callable(build_kb):
        markup = build_kb(int(uid), deals_sorted)  # type: ignore[misc]
    else:
        markup = globals().get("_build_dashboard_kb_v2")(int(uid), deals_sorted)  # type: ignore[misc]

    mid2 = await globals().get("update_my_games_buttons_only")(int(uid), markup)  # type: ignore[misc]
    ok = bool(mid2)
    logger.info("[my_games.patch] after_success: uid=%s deal=%s mid=%s ok=%s", uid, deal_id, mid2, ok)
    return ok



# ════════════════════════════════════════════════════════════════════
# [7] HANDLERS
# ════════════════════════════════════════════════════════════════════
from aiogram import Bot
from aiogram import types as _types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import contextlib

@router.message(Command("my_games"))
@router.message(lambda m: _is_my_games_btn(getattr(m, "text", None)))
async def my_games_handler(message: types.Message) -> None:
    """
    Точка входа в «🎲 Мои игры».
    ВАЖНО: перед отрисовкой чистим ЛС, а в ветке «игр нет» сохраняем отправленное
    сообщение в state.last_user_messages[uid], чтобы следующий «пылесос» его удалил.
    """
    uid = message.from_user.id

    # Закрепим контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    # Чистим предыдущую группу сообщений:
    #  • отчёт/детали — СТИРАЕМ (suppress_report_keep=True),
    #  • главное меню — БЕРЕЖЁМ (добавляем в keep вручную).
    prev_suppress = bool(getattr(state, "suppress_report_keep", False))
    setattr(state, "suppress_report_keep", True)
    try:
        keep_ids = []
        with contextlib.suppress(Exception):
            menu_mid = get_menu_message_id(uid)
            if isinstance(menu_mid, int) and menu_mid > 0:
                keep_ids.append(menu_mid)
        await _vacuum_safe(uid, keep=keep_ids)
    finally:
        setattr(state, "suppress_report_keep", prev_suppress)

    try:
        deals_all = await get_amocrm_deals()
        deals = _visible_deals_for_user(uid, deals_all)
    except Exception as e:
        logger.error("[my_games:handler] get_amocrm_deals failed: %s", e)
        sent = await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        # трекаем, чтобы последующий вакуум удалил
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [sent]
        with contextlib.suppress(Exception):
            await message.delete()
        return

    if deals:
        await _send_dashboard(uid, deals)
    else:
        # «Игр нет»: отправляем ОДНО сообщение и записываем его в трекер
        sent = await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [sent]

    # Не критично: удаляем входящее «кнопочное» сообщение пользователя
    with contextlib.suppress(Exception):
        await message.delete()


@router.callback_query(lambda c: (c.data or "").startswith(DETAILS_PREFIX))
async def _open_my_game_details_cb(callback: types.CallbackQuery) -> None:
    """Открывает детали по кнопке «mygame_details_{deal_id}»."""
    uid = callback.from_user.id

    # Закрепим контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    # Попробуем вытащить саму сделку (если не найдём — перейдём на прямой рендер через show_my_game_details)
    try:
        deal_id = int((callback.data or "").split("_")[-1])
    except Exception:
        with contextlib.suppress(Exception):
            await callback.answer("Не удалось открыть детали.", show_alert=True)
        logger.warning("[my_games] open details failed: bad deal_id in %r", callback.data)
        return

    deal: Optional[Dict] = None
    try:
        # сначала смотрим, что уже рисовали пользователю
        deal = next(
            (d for d in (getattr(state, "games_by_user", {}) or {}).get(uid, [])
             if int(d.get("id") or 0) == deal_id),
            None,
        )
        # если нет — делаем свежую выборку видимых игр и берём оттуда
        if not deal:
            deal = next(
                (d for d in _visible_deals_for_user(uid, await get_amocrm_deals())
                 if int(d.get("id") or 0) == deal_id),
                None,
            )
    except Exception as e:
        logger.debug("[my_games] details fetch via visible_deals failed: %s", e)
        deal = None

    if deal:
        await _send_details(uid, deal)
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    # Фолбэк: отдаём управление стандартному рендеру деталей (он сам выполнит предварительный vacuum)
    try:
        await show_my_game_details(callback)
    except Exception as e:
        with contextlib.suppress(Exception):
            await callback.answer("Не удалось открыть детали.", show_alert=True)
        logger.warning("[my_games] open details failed (fallback): %s", e)


@router.callback_query(lambda c: c.data == "mygames_back")
async def cb_back(callback: types.CallbackQuery) -> None:
    """Вернуться к списку «Мои игры»."""
    uid = callback.from_user.id
    try:
        state.ui_context.setdefault(uid, "my_games")
    except Exception:
        pass

    deals_cached = (getattr(state, "games_by_user", {}) or {}).get(uid, [])
    if deals_cached:
        await _send_dashboard(uid, deals_cached)
        await callback.answer()
        return

    # Если кэша нет — запрашиваем и действуем как в handler'е, критично: трекаем «игр нет»
    prev_suppress = bool(getattr(state, "suppress_report_keep", False))
    setattr(state, "suppress_report_keep", True)
    try:
        keep_ids = []
        with contextlib.suppress(Exception):
            menu_mid = get_menu_message_id(uid)
            if isinstance(menu_mid, int) and menu_mid > 0:
                keep_ids.append(menu_mid)
        await _vacuum_safe(uid, keep=keep_ids)
    finally:
        setattr(state, "suppress_report_keep", prev_suppress)

    try:
        deals_all = await get_amocrm_deals()
        deals = _visible_deals_for_user(uid, deals_all)
    except Exception as e:
        logger.error("[my_games:back] get_amocrm_deals failed: %s", e)
        sent = await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [sent]
        await callback.answer()
        return

    if deals:
        await _send_dashboard(uid, deals)
    else:
        sent = await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [sent]

    await callback.answer()


@router.callback_query(
    lambda c: c.data
    and c.data.startswith("mygame_")
    and c.data not in {"mygames_back"}
    and not c.data.startswith(REPORT_PREFIX)
    and not c.data.startswith(SWAP_PREFIX)
    and not c.data.startswith(DETAILS_PREFIX)
)
async def cb_ack_misc(callback: types.CallbackQuery) -> None:
    """Прочие mygame_* коллбэки — просто ACK (совместимость)."""
    try:
        state.ui_context[callback.from_user.id] = "my_games"
    except Exception:
        pass
    await callback.answer()



# [7.2] HANDLER: SWAP («Замена»/«Попросить замену») — полноценная реализация
# ════════════════════════════════════════════════════════════════════
from contextlib import suppress
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from aiogram import Bot, types as _types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.state import state
from core.utils import resolve_notify_chat_id, team_bulleted_lines
from services import amocrm as _amo  # type: ignore
from services.amocrm import update_amocrm_tags, update_deal_status  # type: ignore

def _swap_role_alias(key: str) -> str:
    k = (key or "").lower()
    if k.startswith("lead") or k == "main": return "main"
    if k.startswith("assist"):              return "assist"
    if "admin" in k:                        return "admin"
    if "trainee" in k or "intern" in k or "стаж" in k: return "trainee"
    return k

def _swap_tag_uid(val: Any) -> Optional[int]:
    try:
        s = str(val or "")
        if "|" in s:
            return int(s.rsplit("|", 1)[-1])
    except Exception:
        pass
    return None

def _swap_label(val: Any) -> str:
    s = str(val or "").strip()
    return s.split("|", 1)[0].strip() if s else ""

def _swap_find_slot_for_user(deal_id: int, uid: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (slot_key, label_without_uid) из locked_distribution для данного пользователя.
    Поддерживает строки и коллекции в значениях слотов.
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
       or {}
    if not isinstance(raw, dict):
        return (None, None)

    def _belongs(v: Any) -> bool:
        if v is None:
            return False
        try:
            return int(_swap_tag_uid(v) or -1) == int(uid)
        except Exception:
            return False

    # lead*/assistant* — ищем конкретный слот
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}:
            seq: Iterable[Any] = v if isinstance(v, (list, tuple, set)) else [v]
            for item in seq:
                if _belongs(item):
                    return (k, _swap_label(item))
    return (None, None)

async def _swap_remove_current_role_tag(deal_id: int, slot_key: Optional[str]) -> None:
    """
    Снимает подтверждающий тег из CRM именно для данного slot_key.
    Реализация зависит от backend services.amocrm.update_amocrm_tags:
    передаём пустую строку в нужный слот — бэкенд удаляет/очищает его.
    """
    try:
        key = str(slot_key or "tag")
        payload: Dict[str, Dict[str, str]] = {str(int(deal_id)): {key: ""}}
        await update_amocrm_tags(payload)  # type: ignore[arg-type]
    except Exception:
        # безопасно молчим — отсутствие тега не критично
        pass

def _swap_restore_from_finished(deal_id: int) -> None:
    """
    Возвращает замки из finished_* обратно в активные locked_* и чистит finished_* записи.
    Ничего не трогает, если активные замки уже есть.
    """
    try:
        locked = getattr(state, "locked_distribution", {}) or {}
        finished_ld = getattr(state, "finished_locked_distribution", {}) or {}
        finished_dc = getattr(state, "finished_distribution_cache", {}) or {}
        key_int, key_str = int(deal_id), str(deal_id)

        if key_int not in locked and key_str not in locked:
            src = finished_ld.pop(key_int, None)
            if src is None:
                src = finished_ld.pop(key_str, None)
            if isinstance(src, dict):
                locked[key_int] = src
                state.locked_distribution = locked  # type: ignore[assignment]

        # очистим кэши finished_*, чтобы игра снова участвовала в цикле
        with suppress(Exception):
            finished_dc.pop(key_int, None)
            finished_dc.pop(key_str, None)
    except Exception:
        pass

async def _swap_announce_and_button(deal_id: int) -> None:
    """
    Объявление в общий чат + кнопка «Откликнуться».
    Состав берём из актуальных slot'ов locked_distribution; рендер через SSOT team_bulleted_lines.
    """
    bot = Bot.get_current()
    try:
        chat_id = resolve_notify_chat_id(bot)  # предпочтительно
    except TypeError:
        chat_id = resolve_notify_chat_id()     # совместимость
    if chat_id is None:
        return

    # Заголовок/дата/время — используем уже имеющийся helper блока [3.3]
    title, date_s, time_s = ("Сделка #{0}".format(int(deal_id)), "", "")
    with suppress(Exception):
        _title_date_time = globals().get("_title_date_time")
        if callable(_title_date_time):
            title, date_s, time_s = _title_date_time(int(deal_id))  # type: ignore[misc]

    # Слоты состава
    slots = (
        (getattr(state, "locked_distribution", {}) or {}).get(int(deal_id))
        or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id))
        or {}
    )
    lines: list[str] = []
    if isinstance(slots, dict) and slots:
        with suppress(Exception):
            lines = await team_bulleted_lines(slots)

    head = f"🔁 Запрошена замена на «{title}» — {date_s} {time_s}".strip()
    body = "\n".join(lines) if lines else ""
    text = "\n".join(p for p in (head, body) if p)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖐 Откликнуться", callback_data=f"{globals().get('RESPOND_PREFIX','swap_accept_')}{int(deal_id)}")]
        ]
    )
    await bot.send_message(chat_id, text, reply_markup=kb)

@router.callback_query(lambda c: c.data and c.data.startswith(SWAP_PREFIX))
async def _cb_swap_request(callback: _types.CallbackQuery) -> None:
    """
    «Замена»: снимает тег текущей роли, переводит сделку в «Бронь»,
    возвращает замки из finished_* в locked_*, объявляет в общий чат и
    вешает кнопку «Откликнуться». Мягко редравит дашборд.
    """
    uid = int(callback.from_user.id)
    try:
        deal_id = int((callback.data or "").split("_")[-1])
    except Exception:
        with suppress(Exception):
            await callback.answer("Некорректные данные.", show_alert=True)
        return

    # slot_key и подпись из active-locked
    slot_key, label = _swap_find_slot_for_user(deal_id, uid)
    # 1) снять подтверждающий тег (если был)
    await _swap_remove_current_role_tag(deal_id, slot_key)

    # 2) перевести статус обратно в «Бронь»
    with suppress(Exception):
        bron = str(getattr(settings, "BRON_STATUS_ID", "") or "")
        if bron:
            await update_deal_status(int(deal_id), bron)  # type: ignore[arg-type]

    # 3) вернуть игру в цикл: перенос замков из finished_* и очистка тамошних следов
    _swap_restore_from_finished(int(deal_id))

    # 4) объявление в рабочем чате + кнопка «Откликнуться»
    with suppress(Exception):
        await _swap_announce_and_button(int(deal_id))

    # 5) мягкий редрав дашборда
    with suppress(Exception):
        if callable(globals().get("_soft_redraw_my_games")):
            await globals().get("_soft_redraw_my_games")(uid)  # type: ignore[misc]

    with suppress(Exception):
        await callback.answer("Запрос на замену отправлен ✅")



# ════════════════════════════════════════════════════════════════════
# [8] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    now = MSK_TZ.localize(datetime.now())
    dummy_bron = {
        "id": 1,
        "game_name": "Quest Room",
        "event_datetime": now,
        "status_id": BRON_STATUS_ID,
        "team_leads": [{"id": "123"}],
        "players": "2-6",
        "tags": [],
    }
    dummy_done = {
        "id": 2,
        "name": "Another Game",
        "event_datetime": now,
        "status_id": OK_STATUS_ID,
        "team_leads": [],
        "players": "5-8",
        "tags": [{"name": "Иван И.2"}],
    }

    # эмулируем слоты, как пишет polls_distribution
    state.locked_distribution = {
        1: {"lead1": "Иван И..1|123", "assistant1": "Пётр П..2|456", "admin": "Анна А..Адм|789"},
        2: {"lead1": "Иван И..1|123"},
    }
    state.assigned_index = {123: {1, 2}, 456: {1}, 789: {1}}
    state.games_by_user = {}

    assert _is_user_assigned_current(123, dummy_bron) is True
    assert _assigned_role_from_state(123, 1) == "main"
    assert _assigned_role_from_state(456, 1) == "assist"
    assert _assigned_role_from_state(789, 1) == "admin"

    assert _wanted_status(dummy_bron) and _wanted_status(dummy_done)

    assert _is_my_games_btn("🎲 Мои игры")
    assert _is_my_games_btn("🎲\u00A0Мои игры")
    assert _is_my_games_btn("🎲\uFE0F\u00A0Мои игры")
    assert _is_my_games_btn("\uFEFF🎲 Мои игры")

    print("handlers.my_games ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _log
    _log.basicConfig(level=_log.DEBUG)
    asyncio.run(_test())