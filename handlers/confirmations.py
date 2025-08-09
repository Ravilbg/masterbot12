# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Утверждение состава руководителем и подтверждение участия ведущими.
Полный цикл: «утверждение» → «подтверждение» → перевод сделки в AmoCRM.

Версия v14.5 · 2025-08-09
──────────────────────────────────────────────────────────────────────────────
• Добавлен блок [13.9] — approve_distribution_handler.
• Подтверждение участия (блок [14.0]) теперь использует state.confirmed_distribution.
• После утверждения руководителем всем назначенным ведущим доступна игра в «Мои игры» с кнопкой «Подтвердить».
• После подтверждения всеми — перевод сделки в «Завершение сделки», удаление из цикла опроса, обновление дашбордов.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Optional, Dict, List

from aiogram import Router
from aiogram.types import CallbackQuery

from core.config import settings
from core.db import get_user_info
from core.state import state
from handlers.poll_details import refresh_deal_details
from handlers.my_games import redraw_my_games
from handlers.polls_lifecycle import _is_deal_ready, _sync_leader_report
from services.amocrm import update_amocrm_tags, update_deal_status

router = Router()
logger = logging.getLogger(__name__)

CONFIRM_PREFIX = "confirm_participation_"
APPROVE_PREFIX = "approve_distribution_"

# ███ [1] ВСПОМОГАТЕЛЬНЫЕ
# --------------------------------------------------------------------
def _leaders_chat_id() -> int:
    return (
        getattr(settings, "LEADERS_CHAT_ID", None)
        or getattr(settings, "leaders_chat_id", None)
        or getattr(settings, "ADMIN_CHAT_ID", None)
        or getattr(settings, "admin_chat_id", None)
    )

async def _user_full_name(uid: int) -> str:
    ui = await get_user_info(uid) or {}
    fn = ui.get("first_name", "") or ""
    ln = ui.get("last_name", "") or ""
    if ln:
        ln = f"{ln[:1]}."
    return f"{fn} {ln}".strip()

def _tag_for_user(full_name: str, role: str) -> str:
    role = role.lower()
    if role == "main":
        return f"{full_name}.1"
    if role == "assist":
        return f"{full_name}.2"
    if role == "admin":
        return f"{full_name}.Ад"
    return full_name

# [2] УТВЕРЖДЕНИЕ СОСТАВА
@router.callback_query(lambda c: c.data.startswith(APPROVE_PREFIX))
async def approve_distribution_handler(callback: CallbackQuery) -> None:
    try:
        deal_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка формата кнопки.", show_alert=True)
        return

    uid = callback.from_user.id
    details = await refresh_deal_details(uid, deal_id)
    if not details or "roles" not in details:
        await callback.answer("Нет данных о составе.", show_alert=True)
        return

    state.confirmed_distribution = getattr(state, "confirmed_distribution", {})
    state.confirmed_distribution[deal_id] = {
        "main":   list(details["roles"].get("main", [])),
        "assist": list(details["roles"].get("assist", [])),
        "admin":  list(details["roles"].get("admin", [])),
    }

    title = getattr(state, "deal_titles", {}).get(deal_id, f"Сделка #{deal_id}")
    try:
        await callback.message.bot.send_message(
            _leaders_chat_id(),
            f"📢 Состав команды на игру «{title}» утверждён.\n"
            f"Подтвердите своё участие в личном кабинете."
        )
    except Exception as e:
        logger.warning("[approve_distribution] notify leaders failed: %s", e)

    await callback.answer("Состав утверждён ✅")


# [3] ПОДТВЕРЖДЕНИЕ УЧАСТИЯ
@router.callback_query(lambda c: c.data.startswith(CONFIRM_PREFIX))
async def confirm_participation_handler(callback: CallbackQuery) -> None:
    try:
        deal_id = int(callback.data.rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Ошибка: неизвестный формат кнопки.", show_alert=True)
        return

    uid = callback.from_user.id
    dist = getattr(state, "confirmed_distribution", {}).get(deal_id)
    if not dist:
        await callback.answer("Нет распределения для этой игры.", show_alert=True)
        logger.error("[confirm] confirmed_distribution not found for deal=%d", deal_id)
        return

    role = None
    for r in ("main", "assist", "admin"):
        if uid in dist.get(r, []):
            role = r
            break
    if not role:
        await callback.answer("Вы не назначены на эту игру.", show_alert=True)
        return

    # Тег → AmoCRM (исправленный интерфейс)
    full_name = await _user_full_name(uid)
    tag = _tag_for_user(full_name, role)
    ok = await update_amocrm_tags({str(deal_id): {"confirm": tag}})
    if not ok:
        await callback.answer("Не удалось записать подтверждение в CRM.", show_alert=True)
        logger.error("[confirm] update_amocrm_tags failed: deal=%d tag=%s", deal_id, tag)
        return

    await callback.answer("Участие подтверждено ✅")

    # Сообщение в чат
    try:
        title = getattr(state, "deal_titles", {}).get(deal_id, f"Сделка #{deal_id}")
        await callback.message.bot.send_message(
            _leaders_chat_id(),
            f"✅ {full_name} подтвердил выход на игру «{title}».",
        )
    except Exception as e:
        logger.warning("[confirm] notify leaders chat failed: %s", e)

    # Локальное подтверждение → проверка «все подтвердили?»
    try:
        state.confirmed.setdefault(deal_id, set()).add(uid)
        required = set(dist.get("main", [])) | set(dist.get("assist", [])) | set(dist.get("admin", []))
        if required and required.issubset(state.confirmed.get(deal_id, set())):
            # Переводим сделку в «Завершение сделки» по ID статуса!
            from handlers.polls_lifecycle import _sync_leader_report
            from services.amocrm import update_deal_status
            try:
                await update_deal_status(deal_id, settings.SUCCESSFUL_STATUS_ID)
            except Exception as e:
                logger.error("[confirm] update_deal_status failed: %s", e)

            # Убираем сделку из активного опроса
            state.current_poll_deals = [d for d in state.current_poll_deals if d.get("id") != deal_id]
            await _sync_leader_report()

            # Проверяем автозавершение цикла, если игр не осталось
            try:
                from handlers.polls_lifecycle import _check_cycle_finished
                await _check_cycle_finished(callback.from_user.id)
            except Exception:
                pass

        # Обновляем «Мои игры» для пользователя
        await redraw_my_games(uid)
    except Exception as e:
        logger.error("[confirm] post-actions failed: %s", e)


# ███ [4] ТЕСТЫ
# --------------------------------------------------------------------
async def _test():
    assert isinstance(_leaders_chat_id(), int) or _leaders_chat_id() is None
    assert _tag_for_user("Иван П.", "main").endswith(".1")
    assert _tag_for_user("Иван П.", "assist").endswith(".2")
    assert _tag_for_user("Иван П.", "admin").endswith(".Ад")
    print("tests passed")

if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())

# История изменений:
# v14.5 · 2025-08-09 — добавлен блок утверждения состава [13.9], цикл подтверждения работает по утверждённому составу.
