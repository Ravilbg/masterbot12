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


@router.callback_query(lambda c: c.data and c.data.startswith("mygame_"))
async def show_my_game_details(callback: types.CallbackQuery) -> None:
    """
    Детали игры в «🎲 Мои игры»:
      • подробности из CRM (дата, время, место, пакет, игроки, статус);
      • утверждённый состав из state.locked_distribution (слоты lead*/assistant*/admin/trainee);
      • кнопка «✅ Подтвердить участие» — при статусе «Бронь» ИЛИ «Предварительная заявка», если назначен;
      • кнопка «🔁 Замена» — после ЛИЧНОГО подтверждения пользователя (даже если вся команда ещё не подтвердила),
        а также при статусе «Завершение сделки»;
      • кнопка «📝 Написать отчёт» — после наступления даты и времени игры.
      • повторно кнопку подтверждения не показываем, если подтверждение уже учтено локально/по тегам.

    ФИКС «пылесоса»: перед рендером деталей мягко удаляем sticky-дашборд (vacuum с ignore_sticky=True),
    затем все отправленные сообщения аккумулируются и сохраняются в state.last_user_messages[uid] — 
    следующий vacuum их удалит гарантированно.

    Изм. 2025-08-29 — корректное время: приоритет event_time (нормализация до HH:MM), 
    затем — время из event_datetime (игнор «00:00»), иначе «—».
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
    if not time_s:
        if hasattr(event_dt, "strftime"):
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

    # Состав из locked_distribution (источник правды — новые "слоты")
    locked_all = (getattr(state, "locked_distribution", {}) or {})
    dist: Dict[str, str] = {}
    if isinstance(locked_all.get(deal_id), dict):
        dist = locked_all[deal_id]
    elif isinstance(locked_all.get(str(deal_id)), dict):
        dist = locked_all[str(deal_id)]  # pragma: no cover

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

    # Роль пользователя (для кнопок)
    role = _assigned_role_from_state(uid, deal_id)  # main/assist/admin/None

    # confirmed по факту: теги ИЛИ локальный state.pending_confirmations
    confirmed_by_tags = _has_confirmation_tag(deal, uid)
    confirmed_local = _is_locally_confirmed(deal_id, uid)
    confirmed = confirmed_by_tags or confirmed_local

    can_confirm = (role in {"main", "assist", "admin"}) and ((status_id == bron_id) or prelim) and (not confirmed)
    # «Замена» после подтверждения или при статусе «Завершение сделки»
    can_swap = (role in {"main", "assist", "admin"}) and (confirmed or status_id == ok_id)

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
# [3.3] NOTIFY: announce_if_all_confirmed — корректное время в уведомлении
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
    • Дополняет нули до 5 символов ('9:0' → '09:00', '9' → '09:00', '930' → '09:30').
    • Пустое/мусор → '' (ничего не подставляем).
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
        return f"{int(hh):02d}:{int(mm):02d}"
    parts = s.split(":", 1)
    try:
        hh = int(parts[0]) if parts[0] else 0
        mm = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        return f"{hh:02d}:{mm:02d}"
    except Exception:
        return ""

def _title_date_time(did: int) -> Tuple[str, str, str]:
    """
    Возвращает (title, date_s, time_s) для уведомления:
    • title: game_name/name → 'Сделка #id' (фолбэк)
    • date:  event_datetime→'%d.%m.%Y' | event_date | deals_index['date'] | ''
    • time:  event_time (нормализованный) приоритетнее; иначе время из event_datetime,
             если оно не '00:00'; иначе deals_index['time'] (нормализованный) | ''.
    """
    title = f"Сделка #{did}"
    date_s, time_s = "", ""

    # current_poll_deals
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) != did:
                continue
            title = str(d.get("game_name") or d.get("name") or title)
            if d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                date_s = d["event_datetime"].strftime("%d.%m.%Y")
            else:
                date_s = str(d.get("event_date") or "") or date_s

            t_from_field = _normalize_time_str(str(d.get("event_time") or ""))
            if t_from_field:
                time_s = t_from_field
            else:
                if d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                    t_dt = d["event_datetime"].strftime("%H:%M")
                    time_s = "" if t_dt == "00:00" else t_dt
            break

    # deals_index фолбэк
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(did) \
            or (getattr(state, "deals_index", {}) or {}).get(str(did)) \
            or {}
        if not title or title.startswith("Сделка #"):
            title = str(meta.get("title") or title)
        if not date_s:
            date_s = str(meta.get("date") or "")
        if not time_s:
            time_s = _normalize_time_str(str(meta.get("time") or ""))
    return title, date_s, time_s

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

        # заголовок/дата/время
        title, date_s, time_s = _title_date_time(did)

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
            chat_id = resolve_notify_chat_id()     # фолбэк (старые сборки)
        if chat_id is None:
            logger.warning("[my_games] notify chat not resolved; skip announce")
            return

        # текст уведомления
        head = f"🎮 «{title}» — {date_s} {time_s}".strip()
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



# ════════════════════════════════════════════════════════════════════
# [3.4] Дашборд «Мои игры» — мягкий редрав и кнопки действий
# Версия 3.4.5 · 2025-08-28
# ────────────────────────────────────────────────────────────────────
# ИЗМЕНЕНО:
# • _build_dashboard_kb больше не зависит от внешних хелперов
#   make_my_games_confirm_btn_for_row/make_my_games_swap_btn_for_row.
#   Кнопки строятся «на месте» на основе SSOT (locked_distribution),
#   локальной отметки и статусов — как в стабильной версии выше.
# • Сохранены заглушки для Pylance и мягкий редрав без циклических импортов.
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

        # строка действия
        if confirmed or sid == OK_ID:
            kb.row(InlineKeyboardButton(text="🔁 Замена", callback_data=f"{globals().get('SWAP_PREFIX','mygame_swap_')}{did}"))
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
    только state.locked_distribution (никаких legacy-источников/голосов/тегов).

    Возвращает: 'main' | 'assist' | 'admin' | 'trainee' | None
    """
    # Вызов SSOT-хелпера сохраняем для совместимости/телеметрии, но решение принимаем по locked.
    try:
        _ = assigned_role_from_state(uid, deal_id)  # не используем результат, полагаемся на locked
    except Exception:
        pass

    locked_all = (getattr(state, "locked_distribution", {}) or {})

    # поддерживаем int/str ключи
    dist: Optional[Dict[str, Any]] = None
    raw = locked_all.get(deal_id)
    if isinstance(raw, dict):
        dist = raw
    else:
        raw = locked_all.get(str(deal_id))
        if isinstance(raw, dict):
            dist = raw

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
    Дополняем CRM-список сделками из ЗАФИКСИРОВАННОГО распределения,
    чтобы утверждённые игры были видны даже при пустом ответе CRM.

    Источники карточки (по приоритету):
      1) уже в all_deals (CRM),
      2) state.current_poll_deals,
      3) state.games_by_user,
      4) state.poll_details / state.deal_titles (минимальный фолбэк).

    Жёсткая дедупликация по id (by_id).
    """
    # ТОЛЬКО из locked_distribution: никаких legacy-индексов/голосов/тегов
    want_ids: Set[int] = _assigned_deal_ids_from_locked(uid)

    logger.debug("[my_games] augment_with_locked: uid=%s want_ids=%s", uid, sorted(want_ids))

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
      • пользователь назначен И УТВЕРЖДЁН (locked_distribution/SSOT);
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
        if not _wanted_status(d):
            continue

        # Единственный валидный критерий — утверждение через locked_distribution
        if not _assigned_role_via_locked(uid, did):
            continue

        out.append(d)
        seen.add(did)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[my_games] visible_deals uid=%s -> %s", uid, [int(x.get("id") or 0) for x in out])

    return out


# История изменений [4]:
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

# ███ [5] DASHBOARD / DETAILS — липкий дашборд + пылесос как в отчёте
# Версия 5.7.3 · 2025-08-29
# Изменения:
# • Добавлен sticky-слот дашборда «Мои игры» (state.my_games_dashboard).
# • _send_dashboard теперь редактирует существующее сообщение (edit_message_text/…),
#   а не удаляет/создаёт заново. Это устраняет «пустые оболочки» в ленте.
# • _vacuum_safe сохраняет sticky и мягко чистит хвосты деталей (как в отчёте).
# • Добавлен update_my_games_buttons_only — «тихая» перекраска кнопок без текста.
# • _build_dashboard_kb_v2 не зависит от внешних make_my_games_*; SSOT-логика внутри.
# • NEW: безопасная сортировка по timestamp (совместима с naive/aware datetime).
import logging
from contextlib import suppress
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Callable, cast
from datetime import datetime

from aiogram import Bot, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.state import state
from core.utils import truncate

logger = logging.getLogger(__name__)

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
    """Сообщения, которые нужно сохранить при пылесосе (как в отчёте)."""
    keep: List[int] = []
    sticky = get_my_games_dashboard(int(uid))
    if isinstance(sticky, int):
        keep.append(sticky)
    for mid in extra_msg_ids:
        try:
            m2 = int(mid)
        except Exception:
            continue
        if m2 not in keep:
            keep.append(m2)
    return keep

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

# ── общий пылесос ЛС (сохраняет sticky) ─────────────────────────────
async def _vacuum_safe(uid: int, keep: Optional[List[Any]] = None, *, ignore_sticky: bool = False) -> None:
    """
    Пылесос ЛС для «Моих игр».
    • Сохраняет sticky-дашборд (если ignore_sticky=False).
    • Чистит хвосты деталей отчёта перед/после вакуума.
    • Использует core.utils.vacuum_private с поддержкой разных сигнатур.
    """
    from aiogram import types as _types
    # предварительно уберём детали
    with suppress(Exception):
        res = _vacuum_poll_details_blocks(int(uid))
        if hasattr(res, "__await__"):
            await res

    keep_ids: List[int] = []
    # нормализуем входные keep
    for k in keep or []:
        with suppress(Exception):
            if isinstance(k, _types.Message):
                keep_ids.append(int(k.message_id))
            elif isinstance(k, int):
                keep_ids.append(int(k))
    # приклеим sticky
    if not ignore_sticky:
        for mid in keep_for_vacuum(int(uid)):
            if mid not in keep_ids:
                keep_ids.append(mid)

    bot = Bot.get_current()

    # основной путь — vacuum_private
    with suppress(Exception):
        from core.utils import vacuum_private as _vacuum  # type: ignore
        try:
            await _vacuum(bot, int(uid), keep=keep_ids)   # (bot, uid, keep)
        except TypeError:
            try:
                await _vacuum(int(uid), keep=keep_ids)    # (uid, keep)
            except TypeError:
                await _vacuum(int(uid))                    # (uid,)
        # хвостовая чистка деталей
        with suppress(Exception):
            res2 = _vacuum_poll_details_blocks(int(uid))
            if hasattr(res2, "__await__"):
                await res2
        return

    # фолбэк — старый пылесос
    with suppress(Exception):
        from core.utils import delete_previous_private_messages as _old  # type: ignore
        try:
            await _old(bot, int(uid), keep=keep_ids)
        except TypeError:
            try:
                await _old(int(uid), keep=keep_ids)
            except TypeError:
                await _old(int(uid))
        with suppress(Exception):
            res3 = _vacuum_poll_details_blocks(int(uid))
            if hasattr(res3, "__await__"):
                await res3

# ── построение клавиатуры дашборда (v2) ─────────────────────────────
def _build_dashboard_kb_v2(uid: int, deals_sorted: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    ВНИМАНИЕ: кнопки «✅ Подтвердить»/«🔁 Замена» строятся ЛОКАЛЬНО по SSOT,
    без внешних make_my_games_* (см. [3.4] для идентичной логики).
    Это гарантирует наличие нужных кнопок в дашборде.
    """
    from core.config import settings as _cfg

    BRON_ID: str   = str(getattr(_cfg, "BRON_STATUS_ID", ""))
    OK_ID: str     = str(getattr(_cfg, "SUCCESSFUL_STATUS_ID", ""))
    PRELIM_ID: str = str(getattr(_cfg, "PRELIM_STATUS_ID", getattr(_cfg, "PRELIMINARY_STATUS_ID", "")) or "")

    # доступ к вспомогательным функциям из модуля (для Pylance — через cast/globals)
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

        # состояние подтверждения (теги CRM + локальная отметка)
        confirmed = False
        if callable(_has_confirmation_tag):
            confirmed = _has_confirmation_tag(d, uid)
        if not confirmed and callable(_is_locally_confirmed_for_redraw):
            with suppress(Exception):
                confirmed = bool(_is_locally_confirmed_for_redraw(did, uid))

        # роль по зафиксированному распределению
        role: Optional[str] = None
        if callable(_assigned_role_from_state):
            with suppress(Exception):
                role = _assigned_role_from_state(uid, did)

        # строка действия: «Замена» если подтверждено или «Заверш.»; иначе «Подтвердить», если назначен и статус допустим
        if confirmed or sid == OK_ID:
            kb.row(InlineKeyboardButton(text="🔁 Замена", callback_data=f"{globals().get('SWAP_PREFIX','mygame_swap_')}{did}"))
        elif role in {"main", "assist", "admin"} and (sid == BRON_ID or status == "Предвар."):
            kb.row(InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"{globals().get('CONFIRM_PREFIX','confirm_role_')}{did}_{role}",
            ))

        # «Отчёт» — отдельной строкой при наступлении даты/времени
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
        await _vacuum_safe(int(uid), keep=[mid])  # заодно подметём хвосты, сохранив дашборд
        return int(mid)
    # если не удалось — сбрасывать sticky не будем; следующая перерисовка создаст новый
    return None

# ── основной вывод/обновление дашборда ──────────────────────────────
async def _send_dashboard(uid: int, deals: List[Dict[str, Any]]) -> None:
    """
    Отрисовывает (или обновляет) список игр:
    • если sticky уже есть — редактируем текст и клавиатуру (без удаления сообщения);
    • если нет — отправляем новое и сохраняем sticky id;
    • пылесосим ЛС, оставляя sticky и убирая детали/старые хвосты.
    """
    bot = Bot.get_current()

    # сортировка карточек: по времени (без сравнения naive/aware datetime)
    def _key(d: Dict[str, Any]) -> tuple:
        dt = _safe_event_dt(d)
        try:
            if dt is None:
                ts = float("inf")
            elif getattr(dt, "tzinfo", None):
                ts = dt.timestamp()
            else:
                ts = dt.replace(tzinfo=MSK_TZ).timestamp()
        except Exception:
            ts = float("inf")
        return (dt is None, ts)

    deals_sorted = sorted(list(deals or []), key=_key)

    # Построение клавиатуры: предпочтительно через [3.4] (_build_dashboard_kb),
    # чтобы коллбэки соответствовали актуальной логике подтверждений; фолбэк — v2.
    build_kb = globals().get("_build_dashboard_kb")
    if callable(build_kb):
        markup = build_kb(int(uid), deals_sorted)  # type: ignore[misc]
    else:
        markup = _build_dashboard_kb_v2(int(uid), deals_sorted)

    text = "🎲 *Мои игры:*"

    # пробуем обновить существующий sticky
    mid = get_my_games_dashboard(int(uid))
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
            # зафиксируем actual message id (Telegram может вернуть Message)
            mid = int(getattr(msg, "message_id", mid))
            set_my_games_dashboard(int(uid), int(mid))
            edited_ok = True
        if edited_ok:
            # мягкий вакуум с сохранением sticky
            await _vacuum_safe(int(uid), keep=[mid])
            # обновим кэши для быстрых редравов/деталей
            (getattr(state, "games_by_user", {}) or {}).setdefault(int(uid), [])
            state.games_by_user[int(uid)] = deals_sorted
            (getattr(state, "last_user_messages", {}) or {}).setdefault(int(uid), [])
            # сохраняем пустой header (sticky используется как источник истины)
            state.last_user_messages[int(uid)] = []
            return

    # если sticky нет или редактирование не удалось — отправим новое
    sent = await bot.send_message(int(uid), text, parse_mode="Markdown", reply_markup=markup)
    mid = int(sent.message_id)
    set_my_games_dashboard(int(uid), int(mid))

    # пылесос с сохранением нового sticky
    await _vacuum_safe(int(uid), keep=[mid])

    # кэши для быстрого редрава/деталей
    (getattr(state, "games_by_user", {}) or {}).setdefault(int(uid), [])
    state.games_by_user[int(uid)] = deals_sorted
    (getattr(state, "last_user_messages", {}) or {}).setdefault(int(uid), [])
    state.last_user_messages[int(uid)] = [sent]

# ── детали из списка (без накопления хвостов) ───────────────────────
async def _send_details(uid: int, deal: Dict[str, Any]) -> None:
    """
    Открывает карточку деталей «Мои игры».
    • Перед показом НЕ вызываем локальный vacuum: предварительную уборку sticky/хвостов
      выполняет сам show_my_game_details (см. [2.1]).
    • Основной рендер делаем через show_my_game_details(fake_cb) (см. блок [2.1]).
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
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к списку", callback_data="mygames_back")
    await bot.send_message(int(uid), text, reply_markup=kb.as_markup())







# [5.2] CROSS-MODULE VACUUM — удаление и обнуление реестров poll_details
from contextlib import suppress
from typing import Any, Dict, Iterable, Set

async def _vacuum_poll_details_blocks(uid: int) -> None:
    """
    Жёсткая очистка «деталей отчёта опроса» у пользователя:
      • пытаемся вызвать публичный API из handlers.poll_details (если он есть),
      • иначе — best-effort: находим все известные реестры в state, удаляем сообщения
        и обнуляем записи для данного uid, чтобы не оставались «хвостовые» индексы.
    Никогда не пробрасывает исключения наружу.
    """
    bot = Bot.get_current()
    u = int(uid)

    # 1) Если в poll_details есть готовый API — используем его.
    with suppress(Exception):
        from handlers.poll_details import forget_all_details_for_user  # type: ignore
        if callable(forget_all_details_for_user):
            await forget_all_details_for_user(u, bot=bot)  # type: ignore[arg-type]
            return

    # 2) Fallback: собираем id всех сообщений из возможных реестров и удаляем вручную.
    def _collect_ints(obj: Any, acc: Set[int]) -> None:
        if obj is None:
            return
        if isinstance(obj, int):
            acc.add(int(obj)); return
        if isinstance(obj, (list, tuple, set)):
            for x in obj: _collect_ints(x, acc); return
        if isinstance(obj, dict):
            for v in obj.values(): _collect_ints(v, acc); return

    ids: Set[int] = set()
    # наиболее вероятные поля, которые использует handlers.poll_details для реестров
    candidate_names = [
        "poll_details_blocks", "poll_detail_blocks",   # списки message_id по сделкам
        "poll_details_index",  "poll_detail_index",    # map ключей 'header/main/...' -> mid
        "pd_blocks", "pd_index",                       # возможные сокращения
        # дополнительно: реестры деталей вне модуля poll_details (совместимость с отчётом/деталями)
        "detail_blocks", "details_blocks",
        "detail_index",  "details_index",
    ]

    registries: Dict[str, Any] = {}
    for name in candidate_names:
        with suppress(Exception):
            val = getattr(state, name)
            registries[name] = val

    # собираем сообщения для конкретного uid из всех найденных структур
    for name, reg in list(registries.items()):
        with suppress(Exception):
            if isinstance(reg, dict):
                # поддержим и ключи int, и str
                node = reg.get(u) or reg.get(str(u))
                _collect_ints(node, ids)

    # удаляем все найденные сообщения (мягко)
    for mid in sorted(ids):
        with suppress(Exception):
            await bot.delete_message(chat_id=u, message_id=int(mid))

    # обнуляем записи для uid во всех известных реестрах, чтобы индекс не «висел»
    for name, reg in list(registries.items()):
        with suppress(Exception):
            if isinstance(reg, dict):
                reg.pop(u, None)
                reg.pop(str(u), None)


# ════════════════════════════════════════════════════════════════════
# [6] PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def redraw_my_games(uid: int) -> None:
    """
    Перерисовывает дашборд «Мои игры» пользователю.

    Быстрый путь:
    • если пользователь уже находится в контексте 'my_games' и доступен мягкий редрав,
      используем _soft_redraw_my_games (без обращения к CRM), чтобы избежать лишних
      сетевых вызовов и визуальных «прыжков».

    Полный путь:
    • во всех остальных случаях запрашиваем список сделок из CRM и строим стандартный дашборд.
    """
    # ── быстрый путь: мягкий редрав, если мы уже в контексте «Моих игр»
    try:
        ctx = (getattr(state, "ui_context", {}) or {}).get(int(uid))
        soft_redraw = globals().get("_soft_redraw_my_games")
        if ctx == "my_games" and callable(soft_redraw):
            await soft_redraw(uid)  # type: ignore[misc]
            return
    except Exception:
        # Любые ошибки в быстрой ветке не фатальны — продолжаем полным путём.
        pass

    # ── полный путь через CRM
    try:
        all_deals = await get_amocrm_deals()
    except Exception as e:
        logger.error("[my_games:redraw] get_amocrm_deals failed: %s", e)
        await _vacuum_safe(uid)
        await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        return

    deals = _visible_deals_for_user(uid, all_deals)
    if deals:
        await _send_dashboard(uid, deals)
    else:
        await _vacuum_safe(uid)
        await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")


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

    # Чистим предыдущую группу сообщений (меню не трогаем)
    await _vacuum_safe(uid)

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
    await _vacuum_safe(uid)
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


# [7.2] HANDLER: SWAP («Замена»/«Попросить замену») — shim
# ════════════════════════════════════════════════════════════════════
"""
DEPRECATION-SHIM.
Истинная реализация «Замены» перенесена в handlers/polls_lifecycle.py:[4.8] и [4.9],
где происходит публикация объявления, ожидание «Откликнуться», пересборка состава
и синхронизация отчётов. Здесь намеренно нет обработчика стандартного префикса.
"""

@router.callback_query(lambda c: c.data and c.data.startswith(f"{SWAP_PREFIX}legacy_"))
async def _cb_swap_request_legacy_noop(callback: types.CallbackQuery) -> None:
    try:
        await callback.answer("Функция замены обновлена. Повторите действие.", show_alert=False)
    except Exception:
        pass
    logger.debug("[my_games.swap] legacy noop called for data=%s", getattr(callback, "data", ""))


# [7.3] HANDLERS: REPORT FLOW — «📝 Написать отчёт»
from services.amocrm import get_deal_by_id, patch_lead, update_deal_status  # type: ignore
try:
    from services.amocrm import _build_cf_patch  # type: ignore
except Exception:
    _build_cf_patch = None  # type: ignore

@router.callback_query(lambda c: c.data and c.data.startswith(REPORT_PREFIX))
async def cb_report_start(callback: _types.CallbackQuery) -> None:
    uid = callback.from_user.id
    deal_id = int((callback.data or "").split("_")[-1])
    state.pending_report = getattr(state, "pending_report", {})
    state.pending_report[uid] = deal_id
    with contextlib.suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    await Bot.get_current().send_message(
        uid,
        "📝 Пришлите текст отчёта одним сообщением. Чтобы отменить — отправьте слово «Отмена».",
    )
    await callback.answer()

@router.message(lambda m: (getattr(state, "pending_report", {}) or {}).get(m.from_user.id))
async def on_report_text(message: types.Message) -> None:
    uid = message.from_user.id
    deal_id = int((getattr(state, "pending_report", {}) or {}).get(uid))
    text = (message.text or "").strip()
    if text.lower() in {"отмена", "cancel", "/cancel"}:
        (getattr(state, "pending_report", {}) or {}).pop(uid, None)
        await message.answer("Отмена. Возвращаюсь к «Моим играм».")
        await redraw_my_games(uid)
        return
    deal = await get_deal_by_id(int(deal_id))
    old_comment = str((deal or {}).get("comment") or "").strip()
    stamp = datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M")
    author = _short_name(uid) or "Ведущий"
    new_comment = (old_comment + "\n\n" if old_comment else "") + f"Отчёт {author} от {stamp}:\n{text}"
    payload = await _build_cf_patch({"comment": new_comment}) if callable(_build_cf_patch) else None
    if not payload or not await patch_lead(int(deal_id), payload):
        await message.answer("⚠️ Не удалось сохранить отчёт в сделке. Попробуйте позже.")
        return
    ok = await update_deal_status(int(deal_id), str(OK_STATUS_ID))
    await message.answer("✅ Отчёт принят и добавлен в сделку." + ("" if ok else " (статус сменить не удалось)"))
    (getattr(state, "pending_report", {}) or {}).pop(uid, None)
    await _soft_redraw_my_games(uid)


# [7.4] POST-CONFIRM UI HOOK (no callback intercept)
# Версия 7.4.4 · 2025-08-29
# Изменения:
# • УБРАНЫ любые локальные обработчики CONFIRM_PREFIX (чтобы не перехватывать подтверждение).
# • Добавлен безопасный UI-патч, который можно вызвать ИЗ handlers/confirmations.py.
# • «Детали»: кнопка мгновенно меняется на «🔁 Замена»; «Дашборд»: мягкий редрав.
from typing import Callable, Optional, cast, TYPE_CHECKING
from contextlib import suppress

# Заглушка для Pylance: получаем sticky-id дашборда из блока [1.4]
if TYPE_CHECKING:
    def get_my_games_dashboard(uid: int) -> Optional[int]: ...
else:
    get_my_games_dashboard = cast(Callable[[int], Optional[int]], globals().get("get_my_games_dashboard"))  # type: ignore

async def mygames_after_confirm_ui_patch(
    uid: int,
    deal_id: int,
    role: str,
    msg: _types.Message | None = None,
) -> None:
    """
    Мягкий локальный патч UI после подтверждения (по желанию вызывающей стороны):
      • если подтверждение пришло из ДЕТАЛЕЙ — меняем кнопку на «🔁 Замена»;
      • если подтверждение пришло из ДАШБОРДА — НЕ редактируем его клавиатуру,
        а выполняем мягкий редрав всего дашборда;
      • отмечаем локально confirmed (для мгновенного отображения) и делаем мягкий редрав списка.

    Ничего не делает с AmoCRM/уведомлениями/статусом — это всё в handlers/confirmations.py.
    """
    try:
        # 1) Понять, откуда пришло подтверждение: из деталей или из самого дашборда
        is_dashboard_msg = False
        try:
            sticky_mid = None
            if callable(get_my_games_dashboard):
                sticky_mid = get_my_games_dashboard(int(uid))
            if msg and getattr(msg, "message_id", None):
                # эвристики: совпадает со sticky-id ИЛИ текст — заголовок дашборда
                is_dashboard_msg = (sticky_mid is not None and int(msg.message_id) == int(sticky_mid)) \
                                   or (isinstance(msg.text, str) and msg.text.strip().startswith("🎲 "))
        except Exception:
            is_dashboard_msg = False

        # 2) Локальная отметка подтверждения (на случай задержки CRM-тегов)
        pc = (getattr(state, "pending_confirmations", {}) or {}).setdefault(int(deal_id), {"confirmed": {}})
        pc.setdefault("confirmed", {}).setdefault(str(role), set()).add(int(uid))

        # 3) Ветвление по контексту:
        #    — если ДЕТАЛИ: изменить кнопку на «🔁 Замена»,
        #    — если ДАШБОРД: не трогаем клавиатуру — делаем мягкий редрав целиком.
        if not is_dashboard_msg:
            # Контекст ДЕТАЛЕЙ: изменить кнопку на «🔁 Замена»
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔁 Замена",
                                                      callback_data=f"{SWAP_PREFIX}{int(deal_id)}")]]
            )
            target = None
            if msg and getattr(msg, "message_id", None):
                target = msg
            else:
                # попытка найти «второе» сообщение деталей в last_user_messages
                msgs = (getattr(state, "last_user_messages", {}) or {}).get(int(uid), [])
                if len(msgs) >= 2 and getattr(msgs[1], "message_id", None):
                    target = msgs[1]
            if target:
                await target.edit_reply_markup(reply_markup=kb)
        # Для дашборда никаких прямых правок клавиатуры НЕ делаем!

        # 4) Мягкий редрав дашборда (и в случае деталей, и в случае дашборда)
        if callable(globals().get("_soft_redraw_my_games")):
            await _soft_redraw_my_games(int(uid))  # type: ignore[misc]
    except Exception:
        # UI-патч не критичен, ошибки гасим.
        pass



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