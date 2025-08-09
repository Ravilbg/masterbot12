# handlers/my_games.py — дашборд «Мои игры»
# ─────────────────────────────────────────────────────────────────────────────
"""
Версия 4.6 · 2025-08-09

Изменения против 4.5:
• Уточнён фильтр назначенности: учитываем только реально назначенных (team_leads).
• Защита от пустых/неполных карточек сделки (event_datetime/name/status).
• Перерисовка «Мои игры» после подтверждения вызывается внешне (confirmations.redraw_my_games),
  но функция redraw_my_games доступна и для ручного вызова.
• Улучшенный лог состояний и кодпоинтов (receive/match/send/details/back).
• Полная поддержка NBSP/FE0F/ZWNBSP в текстовых кнопках.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import contextlib
import logging
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Set

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.state import state
from core.utils import truncate, delete_previous_private_messages
from services.amocrm import get_amocrm_deals

# Важно: префикс коллбэка подтверждения должен совпадать с handlers/confirmations.py
try:
    from handlers.confirmations import CONFIRM_PREFIX  # ожидается "confirm_participation_"
except Exception:  # fallback на случай раннего импорта
    CONFIRM_PREFIX = "confirm_participation_"

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
# «Завершение сделки» — у settings.SUCCESSFUL_STATUS_ID
OK_STATUS_ID: str = str(getattr(settings, "SUCCESSFUL_STATUS_ID", settings.SUCCESSFUL_STATUS_ID))

# ── callback-префиксы ────────────────────────────────────────────────
DETAILS_PREFIX = "mygame_details_"
REPORT_PREFIX  = "mygame_report_"
SWAP_PREFIX    = "mygame_swap_"

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

# ════════════════════════════════════════════════════════════════════
# [3] DOMAIN HELPERS
# ════════════════════════════════════════════════════════════════════
def _confirmed(uid: int, deal_id: int) -> bool:
    """
    Временная локальная отметка подтверждения (если ведётся в state.confirmed).
    Основным источником истины являются теги в AmoCRM (обновляются в confirmations).
    """
    cm: Dict[int, Set[int]] = state.__dict__.setdefault("confirmed", {})
    return uid in cm.get(deal_id, set())


def _is_main_leader(uid: int, deal: Dict) -> bool:
    """Основной ведущий — первый из team_leads."""
    leads = deal.get("team_leads") or []
    try:
        return bool(leads and str(leads[0].get("id")) == str(uid))
    except Exception:
        return False


def _is_user_assigned(uid: int, deal: Dict) -> bool:
    """Пользователь назначен на сделку (в любом слоте team_leads)."""
    leads = deal.get("team_leads") or []
    uid_s = str(uid)
    for t in leads:
        try:
            if str(t.get("id")) == uid_s:
                return True
        except Exception:
            continue
    return False


def _safe_event_dt(deal: Dict) -> Optional[datetime]:
    dt = deal.get("event_datetime")
    if isinstance(dt, datetime):
        return dt
    return None


def _safe_title(deal: Dict) -> str:
    return (deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}").strip()


def _safe_status_id(deal: Dict) -> str:
    return str(deal.get("status_id") or "")


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


def _details_kb(uid: int, deal: Dict, confirmed: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    # Кнопка подтверждения — только для «Бронь» и только если ещё не подтверждено локально.
    if _safe_status_id(deal) == BRON_STATUS_ID and not confirmed and _is_user_assigned(uid, deal):
        kb.button(text="✅ Подтвердить участие",
                  callback_data=f"{CONFIRM_PREFIX}{deal['id']}")

    # Кнопка отчёта — только для основного ведущего
    if _is_main_leader(uid, deal):
        kb.button(text="📝 Написать отчёт",
                  callback_data=f"{REPORT_PREFIX}{deal['id']}")

    # Запрос замены доступен всем назначенным
    if _is_user_assigned(uid, deal):
        kb.button(text="🔄 Попросить замену",
                  callback_data=f"{SWAP_PREFIX}{deal['id']}")

    kb.button(text="← Назад", callback_data="mygames_back")
    kb.adjust(1)
    return kb.as_markup()


def _my_games(uid: int, deals: List[Dict]) -> List[Dict]:
    """
    Возвращает только те сделки, где пользователь назначен (team_leads)
    и статус — «Бронь» или «Завершение сделки».
    """
    wanted = {BRON_STATUS_ID, OK_STATUS_ID}
    out: List[Dict] = []
    for d in deals:
        try:
            if _safe_status_id(d) in wanted and _is_user_assigned(uid, d):
                out.append(d)
        except Exception:
            continue
    return out

# ════════════════════════════════════════════════════════════════════
# [4] DASHBOARD / DETAILS
# ════════════════════════════════════════════════════════════════════
async def _send_dashboard(uid: int, deals: List[Dict]) -> None:
    bot = Bot.get_current()
    kb = InlineKeyboardBuilder()

    # Сортируем по дате мероприятия (неуказанные — в конец)
    def _key(d: Dict):
        dt = _safe_event_dt(d)
        return (dt is None, dt or datetime.max)

    deals_sorted = sorted(deals, key=_key)

    for d in deals_sorted:
        title = truncate(_safe_title(d), 28)
        dt = _safe_event_dt(d)
        date = dt.strftime("%d.%m") if dt else "??.??"
        kb.button(
            text=f"ℹ️ {title} · {date}",
            callback_data=f"{DETAILS_PREFIX}{d['id']}",
        )

    kb.adjust(1)
    await delete_previous_private_messages(uid)
    msg = await bot.send_message(
        uid, "🎲 *Мои игры:*", parse_mode="Markdown", reply_markup=kb.as_markup()
    )
    # Кэшируем последние сделки пользователя и последнее сообщение
    state.games_by_user[uid] = deals_sorted
    state.last_user_messages[uid] = [msg]


async def _send_details(uid: int, deal: Dict) -> None:
    await delete_previous_private_messages(uid)
    conf = _confirmed(uid, deal["id"])
    text = _details_text(deal, conf)
    kb = _details_kb(uid, deal, conf)
    msg = await Bot.get_current().send_message(
        uid, text, parse_mode="Markdown", reply_markup=kb
    )
    state.last_user_messages[uid] = [msg]

# ════════════════════════════════════════════════════════════════════
# [5] PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def redraw_my_games(uid: int) -> None:
    """
    Перерисовывает дашборд «Мои игры» пользователю:
    • если есть назначенные — рисуем список,
    • если нет — очищаем и показываем «Назначенных игр нет».
    """
    try:
        all_deals = await get_amocrm_deals()
    except Exception as e:
        logger.error("[my_games:redraw] get_amocrm_deals failed: %s", e)
        await delete_previous_private_messages(uid)
        await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        return

    deals = _my_games(uid, all_deals)
    if deals:
        await _send_dashboard(uid, deals)
    else:
        await delete_previous_private_messages(uid)
        await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")

# ════════════════════════════════════════════════════════════════════
# [6] HANDLERS
# ════════════════════════════════════════════════════════════════════
@router.message(Command("my_games"))
@router.message(lambda m: _is_my_games_btn(getattr(m, "text", None)))
async def my_games_handler(message: types.Message) -> None:
    uid = message.from_user.id
    try:
        deals = _my_games(uid, await get_amocrm_deals())
    except Exception as e:
        logger.error("[my_games:handler] get_amocrm_deals failed: %s", e)
        await message.answer("⚠️ Не удалось получить список игр.")
        with contextlib.suppress(Exception):
            await message.delete()
        return

    if deals:
        await _send_dashboard(uid, deals)
    else:
        await message.answer("😔 Назначенных игр пока нет.")

    with contextlib.suppress(Exception):
        await message.delete()


@router.callback_query(lambda c: c.data.startswith(DETAILS_PREFIX))
async def cb_details(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        deal_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("⚠️ Ошибочная кнопка.", show_alert=True)
        return

    # Ищем в кэше пользователя, иначе — запрашиваем из CRM
    deal: Optional[Dict] = next(
        (d for d in state.games_by_user.get(uid, []) if d.get("id") == deal_id), None
    )
    if not deal:
        try:
            deal = next(
                (d for d in _my_games(uid, await get_amocrm_deals()) if d.get("id") == deal_id),
                None,
            )
        except Exception as e:
            logger.error("[my_games:details] get_amocrm_deals failed: %s", e)
            deal = None

    if not deal:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    await _send_details(uid, deal)
    await callback.answer()


@router.callback_query(lambda c: c.data == "mygames_back")
async def cb_back(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    if state.games_by_user.get(uid):
        await _send_dashboard(uid, state.games_by_user[uid])
    else:
        await redraw_my_games(uid)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith(REPORT_PREFIX))
async def cb_report_placeholder(callback: types.CallbackQuery) -> None:
    await callback.answer("📝 Отчёт — в разработке.", show_alert=True)


@router.callback_query(lambda c: c.data.startswith(SWAP_PREFIX))
async def cb_swap_placeholder(callback: types.CallbackQuery) -> None:
    await callback.answer("🔄 Замена — в разработке.", show_alert=True)

# ════════════════════════════════════════════════════════════════════
# [7] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    now = MSK_TZ.localize(datetime.now())
    dummy = {
        "id": 1,
        "game_name": "Quest Room",
        "event_datetime": now,
        "status_id": BRON_STATUS_ID,
        "team_leads": [{"id": "123"}],
        "players": "2-6",
    }
    assert _my_games(123, [dummy]) == [dummy], "Пользователь должен видеть свою игру"
    assert _is_main_leader(123, dummy), "Основной ведущий — первый в списке"
    assert not _confirmed(123, 1), "По умолчанию локально не подтверждено"
    assert _is_my_games_btn("🎲 Мои игры")
    assert _is_my_games_btn("🎲\u00A0Мои игры")
    assert _is_my_games_btn("🎲\uFE0F\u00A0Мои игры")
    assert _is_my_games_btn("\uFEFF🎲 Мои игры")
    print("handlers.my_games ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _log
    _log.basicConfig(level=_log.DEBUG)
    asyncio.run(_test())

# История изменений:
# • 2025‑08‑09 — v4.6: усилена устойчивость к данным CRM, уточнены фильтры и кнопки,
#   fallback CONFIRM_PREFIX, логирование и сортировка по дате.
