# handlers/polls_distribution.py
# ─────────────────────────────────────────────────────────────────────────────
"""Ручное управление распределением: подтверждения «+», утверждение игр,
запись тегов в AmoCRM.

Версия v12.94-cycle · 2025-07-23
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Dict, List

from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.state import state
from services.amocrm import update_amocrm_tags
from handlers.confirmations import _get_all_assigned_uids

# ── ленивый импорт polls_lifecycle: функции гарантированно существуют
#    к моменту выполнения, а Pylance больше не ругается на «не определено».
import handlers.polls_lifecycle as plc  # noqa: E402

# локальные алиасы для удобства (оставляем старые имена ─ править остальной код не нужно)
_cancel_reminders      = plc._cancel_reminders
_request_confirmations = plc._request_confirmations
clear_poll_data        = plc.clear_poll_data
_is_deal_ready         = plc._is_deal_ready

logger = logging.getLogger(__name__)
router = Router()

# История изменений:
#   • 2025-07-30 — добавлен ленивый импорт plc + алиасы для устранения предупреждений Pylance


# ════════════════════════════════════════════════════════════════════
# [1] INLINE-КЛАВИАТУРА ПОД ОТЧЁТОМ
# ════════════════════════════════════════════════════════════════════
def distribution_actions_markup() -> InlineKeyboardMarkup:
    """
    Возвращает **пустую** клавиатуру.

    Все «служебные» кнопки удалены из дашборда лидера согласно новому UI-дизайну.
    """
    return InlineKeyboardMarkup(inline_keyboard=[])

# ════════════════════════════════════════════════════════════════════
# [2] ОБЩИЕ УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════
def _compose_tag_report(deal: Dict) -> str:
    """Формирует текст тэгов для игры."""
    dist = state.distribution_cache.get(str(deal["id"]), {})
    if not dist:
        return "_нет распределения_"
    lines: List[str] = []
    for role, tag in dist.items():
        emoji = {
            "lead": "🧭",
            "assistant": "🛟",
            "admin": "🛡️",
        }.get(role.split("1")[0], "👤")
        lines.append(f"{emoji} {role}: {tag or '_—_'}")
    return "\n".join(lines)

# ════════════════════════════════════════════════════════════════════
# [3] HANDLERS: ОБЩИЕ ДЕЙСТВИЯ
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data == "poll_force_confirm_all")
async def poll_force_confirm_all_handler(callback: types.CallbackQuery) -> None:
    if state.manual_confirm_requested:
        await callback.answer("Уже запрошено.", show_alert=True)
        return
    _cancel_reminders()
    await _request_confirmations()
    await callback.answer("Запросили «+» от ведущих.")
    logger.info("[manual] force confirm")


@router.callback_query(lambda c: c.data == "poll_force_finish")
async def poll_force_finish_handler(callback: types.CallbackQuery) -> None:
    await clear_poll_data(callback.from_user.id)
    await callback.answer("Цикл завершён вручную.")
    logger.info("[manual] force finish")


@router.callback_query(lambda c: c.data == "save_distribution")
async def save_distribution_to_amocrm_handler(callback: types.CallbackQuery) -> None:
    if not state.distribution_cache:
        await callback.answer("⚠️ Сначала утвердите распределение.", show_alert=True)
        return

    await callback.answer("⏳ Сохраняю теги…")
    ok = await update_amocrm_tags(state.distribution_cache)
    if ok:
        await callback.answer("✅ Теги сохранены в AmoCRM.", show_alert=True)
        logger.info("[manual] tags saved")
    else:
        await callback.answer("❌ Ошибка при сохранении.", show_alert=True)
        logger.error("[manual] tags save failed")

# ════════════════════════════════════════════════════════════════════
# [4] УТВЕРЖДЕНИЕ ОТДЕЛЬНОЙ ИГРЫ
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data.startswith("approve_deal_"))
async def approve_deal_handler(callback: types.CallbackQuery) -> None:
    did = int(callback.data.split("_")[-1])
    if not state.current_deal_ready.get(did):
        await callback.answer("Минимум ещё не набран.", show_alert=True)
        return

    deal = next(d for d in state.current_poll_deals if d["id"] == did)
    if str(did) not in state.distribution_cache:
        await callback.answer("⚠️ Нет распределения.", show_alert=True)
        return

    caption = (
        f"🎯 *{deal['name']}* — {deal['event_datetime']:%d.%m}\n"
        f"{_compose_tag_report(deal)}"
    )
    msg = await callback.message.answer(caption, parse_mode="Markdown")
    state.pending_plus[msg.message_id] = did
    await callback.answer("Ожидаем «+» от ведущего.")
    logger.info("[manual] approve deal %d", did)

# ════════════════════════════════════════════════════════════════════
# [5] УТВЕРЖДЕНИЕ ВСЕХ ГОТОВЫХ
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data == "approve_all_ready")
async def approve_all_ready_handler(callback: types.CallbackQuery) -> None:
    ready_deals = [d for d in state.current_poll_deals if state.current_deal_ready.get(d["id"])]
    if not ready_deals:
        await callback.answer("Пока нет готовых игр.", show_alert=True)
        return

    cnt = 0
    for deal in ready_deals:
        if str(deal["id"]) not in state.distribution_cache:
            continue
        caption = (
            f"🎯 *{deal['name']}* — {deal['event_datetime']:%d.%m}\n"
            f"{_compose_tag_report(deal)}"
        )
        msg = await callback.message.answer(caption, parse_mode="Markdown")
        state.pending_plus[msg.message_id] = deal["id"]
        cnt += 1

    await callback.answer(f"Отправлено {cnt} запрос(ов) «+».")
    logger.info("[manual] approve all ready (%d)", cnt)

# ════════════════════════════════════════════════════════════════════
# [6] ПРЕДВАРИТЕЛЬНЫЙ ОТЧЁТ (ПО КЛИКУ «👌 Утвердить распределение»)
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data == "distribute_leaders")
async def distribute_leaders_handler(callback: types.CallbackQuery) -> None:
    if not state.distribution_cache:
        await callback.answer("⚠️ Распределение не готово.", show_alert=True)
        return

    report: List[str] = ["📋 *Предварительное распределение:*"]
    for deal in state.current_poll_deals:
        dist = state.distribution_cache.get(str(deal["id"]), {})
        report.append(f"\n🎯 *{deal['name']}* — {deal['event_datetime']:%d.%m}")
        if not dist:
            report.append("_нет распределения_")
            continue
        for role, tag in dist.items():
            emoji = {"lead": "🧭", "assistant": "🛟", "admin": "🛡️"}.get(role.split("1")[0], "👤")
            report.append(f"{emoji} {role}: {tag or '_—_'}")

    await callback.message.answer("\n".join(report), parse_mode="Markdown")
    await callback.answer()

# ════════════════════════════════════════════════════════════════════
# [7] ПЛЕЙСХОЛДЕР «MANUAL_EDIT»
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data == "manual_edit")
async def manual_edit_placeholder(callback: types.CallbackQuery) -> None:
    """Заглушка под будущий визуальный редактор распределения."""
    await callback.answer("Функция в разработке.", show_alert=True)

# ███ [99] _TEST
# --------------------------------------------------------------------
async def _test() -> None:
    """Smoke-тест distribution_actions_markup и _compose_tag_report."""
    # distribution_actions_markup
    markup = distribution_actions_markup()
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard == []

    # _compose_tag_report
    dummy = {"id": 1}
    state.distribution_cache = {}
    assert _compose_tag_report(dummy) == "_нет распределения_"
    state.distribution_cache = {"1": {"lead1": "Иван|1"}}
    report = _compose_tag_report(dummy)
    assert "Иван" in report
    print("handlers/polls_distribution OK")

if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())