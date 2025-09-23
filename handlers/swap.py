# ╔══════════════════════════════════════════════════════════════════╗
# ║ handlers/swap.py — «🔁 Замена» из «Мои игр»                     ║
# ║ Версия 1.1 · 2025-09-23                                          ║
# ╚══════════════════════════════════════════════════════════════════╝
"""
Логика запроса замены из «Моих игр» и отклика из общего чата.

Что входит:
• mygames_swap_{deal_id} — запрос замены самим назначенным участником;
• mygames_swap_accept_{deal_id}_{slot} — первый «🖐 Откликнуться» из общего чата побеждает;
• Проверки по «Светофору» (green для main; green|yellow для assist/admin);
• Обновление locked_distribution, теги в AmoCRM, уведомление «Состав обновлён…»;
• Безопасные фолбэки (сервисы могут отсутствовать в ранних сборках/тестах).

В этой версии добавлена метка срочной замены (state.swap_urgent_candidates).
"""

from __future__ import annotations
import asyncio
import contextlib
import logging
from typing import Any, Dict, Optional, Tuple

from aiogram import Router, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

# SSOT imports (с безопасными фолбэками)
try:
    from core.config import settings
except Exception:
    class _S: BRON_STATUS_ID: Optional[int] = None
    settings = _S()  # type: ignore

try:
    from core.state import state
except Exception:
    class _State:
        locked_distribution: Dict[int, Dict[str, Any]] = {}
        pending_swaps: Dict[int, Dict[str, Any]] = {}
        swap_locks: Dict[int, asyncio.Lock] = {}
        swap_urgent_candidates: Dict[int, int] = {}
    state = _State()  # type: ignore

try:
    from core.utils import resolve_notify_chat_id, team_bulleted_lines, short_name
except Exception:
    def resolve_notify_chat_id() -> Optional[int]:
        return None
    async def team_bulleted_lines(slots: Dict[str, Any]) -> list[str]:
        return [f"• {k}: {v}" for k, v in (slots or {}).items()]
    async def short_name(uid: int) -> str:
        return f"uid:{uid}"

try:
    from services.amocrm import get_deal_by_id, update_amocrm_tags, update_deal_status
except Exception:
    async def get_deal_by_id(deal_id: int) -> Dict[str, Any]:
        return {"id": deal_id, "name": f"Deal #{deal_id}"}
    async def update_amocrm_tags(payload: Dict[str, Dict[str, str]]) -> None:
        return None
    async def update_deal_status(deal_id: int, status_id: str) -> None:
        return None

try:
    from services.gsheets import get_user_status_from_svetofor
except Exception:
    async def get_user_status_from_svetofor(uid: int, game: str) -> str:
        return "green"

from aiogram import Bot

router = Router(name="swap")
log = logging.getLogger(__name__)

# ── Префиксы (уникальны для «Моих игр») ────────────────────────────
_REQ_PREFIX = "mygames_swap_"                 # mygames_swap_{deal_id}
_ACC_PREFIX = "mygames_swap_accept_"          # mygames_swap_accept_{dealId}_{slotKey}


# ── Внутренние утилиты ──────────────────────────────────────────────
def _parse_req(data: str) -> Optional[int]:
    if not data or not data.startswith(_REQ_PREFIX):
        return None
    try:
        return int(data[len(_REQ_PREFIX):])
    except Exception:
        return None

def _parse_acc(data: str) -> Optional[Tuple[int, str]]:
    if not data or not data.startswith(_ACC_PREFIX):
        return None
    try:
        tail = data[len(_ACC_PREFIX):]
        deal_s, slot = tail.split("_", 1)
        return (int(deal_s), slot)
    except Exception:
        return None

def _find_user_slot(dist: Dict[str, Any], uid: int) -> Optional[str]:
    for slot, val in (dist or {}).items():
        items = val if isinstance(val, (list, tuple, set)) else [val]
        for it in items:
            s = str(it or "")
            if s.endswith(f"|{uid}"):
                return slot
    return None

def _role_from_slot(slot: str) -> str:
    s = (slot or "").lower()
    if s.startswith("lead"): return "main"
    if s.startswith("assistant"): return "assist"
    if "admin" in s: return "admin"
    return "assist"


async def _announce_swap(deal: Dict[str, Any], slot: str, who_uid: int) -> None:
    """Публикация объявления в общий чат с кнопкой «🖐 Откликнуться»."""
    chat_id: Optional[int] = None
    with contextlib.suppress(Exception):
        rid = resolve_notify_chat_id()
        chat_id = rid if isinstance(rid, int) else None
    if not chat_id:
        return

    title = str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}")
    date_s = ""
    with contextlib.suppress(Exception):
        if deal.get("event_datetime"):
            date_s = deal["event_datetime"].strftime("%d.%m.%Y")
        else:
            date_s = str(deal.get("event_date") or "")
    time_s = str(deal.get("event_time") or "")
    pkg = str(deal.get("package") or "")
    players = str(deal.get("players_count") or deal.get("players") or "")
    who = await short_name(who_uid)

    text = (
        f"⚠️ {who} просит срочную замену на игру:\n"
        f"• {title}\n"
        f"• {date_s} {time_s}\n"
        f"• Пакет: {pkg}, игроков: {players}\n\n"
        f"Нужен человек на слот: {slot}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖐 Откликнуться",
                              callback_data=f"{_ACC_PREFIX}{int(deal['id'])}_{slot}")]
    ])
    try:
        bot = Bot.get_current()
        await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        log.exception("[swap] announce failed deal=%s slot=%s", deal.get("id"), slot)


# ── HANDLERS ────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith(_REQ_PREFIX))
async def mygames_swap_request(callback: CallbackQuery) -> None:
    """Клик по «🔁 Замена» в «Моих играх»."""
    uid = int(callback.from_user.id)
    deal_id = _parse_req(callback.data or "")
    if not deal_id:
        with contextlib.suppress(Exception):
            await callback.answer("Ошибка кнопки", show_alert=True)
        return

    # Берём утверждённый состав из locked_distribution (SSOT).
    locked = getattr(state, "locked_distribution", {}) or {}
    raw = locked.get(deal_id) or locked.get(str(deal_id)) or {}
    if not isinstance(raw, dict) or not raw:
        with contextlib.suppress(Exception):
            await callback.answer("Игра не найдена.", show_alert=True)
        return

    slot = _find_user_slot(raw, uid)
    if not slot:
        with contextlib.suppress(Exception):
            await callback.answer("Вы не назначены на эту игру.", show_alert=True)
        return

    # Ставим флаг «замена активна» и освобождаем свой слот локально
    pending: Dict[int, Dict[str, Any]] = getattr(state, "pending_swaps", {}) or {}
    pend = pending.get(deal_id, {})
    if pend.get("active"):
        with contextlib.suppress(Exception):
            await callback.answer("Замена уже запрошена — ждём отклик.", show_alert=True)
        return
    pend.update({"active": True, "slot": slot, "who": uid})
    pending[deal_id] = pend
    setattr(state, "pending_swaps", pending)

    raw.pop(slot, None)
    locked[int(deal_id)] = raw
    setattr(state, "locked_distribution", locked)

    # Возвращаем в «Бронь» (если требуется правилами) — безопасно
    with contextlib.suppress(Exception):
        if getattr(settings, "BRON_STATUS_ID", None):
            await update_deal_status(int(deal_id), str(settings.BRON_STATUS_ID))

    # Объявление в общий чат
    deal = await get_deal_by_id(int(deal_id)) or {}
    await _announce_swap(deal, slot, uid)

    # Тихий апдейт клавиатуры в ЛК (перекрасить кнопку)
    with contextlib.suppress(Exception):
        if callback.message and callback.message.reply_markup:
            new_rows = []
            for row in callback.message.reply_markup.inline_keyboard or []:
                nr = []
                for b in row:
                    if b.callback_data == callback.data:
                        nr.append(InlineKeyboardButton(text="⌛ Ждём замену", callback_data="noop"))
                    else:
                        nr.append(b)
                new_rows.append(nr)
            await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows))

    with contextlib.suppress(Exception):
        await callback.answer("Запрос на замену отправлен. Ждём отклик.")


@router.callback_query(lambda c: c.data and c.data.startswith(_ACC_PREFIX))
async def mygames_swap_accept(callback: CallbackQuery) -> None:
    """Первый «🖐 Откликнуться» побеждает, проверяем «Светофор», назначаем."""
    parsed = _parse_acc(callback.data or "")
    if not parsed:
        with contextlib.suppress(Exception):
            await callback.answer("Ошибка кнопки", show_alert=True)
        return
    deal_id, slot = parsed
    uid = int(callback.from_user.id)

    # Один победитель на сделку — lock
    locks: Dict[int, asyncio.Lock] = getattr(state, "swap_locks", {}) or {}
    lock = locks.setdefault(int(deal_id), asyncio.Lock())
    setattr(state, "swap_locks", locks)

    if lock.locked():
        with contextlib.suppress(Exception):
            await callback.answer("Уже занято, спасибо!", show_alert=False)
        return

    async with lock:
        pend = (getattr(state, "pending_swaps", {}) or {}).get(int(deal_id)) or {}
        if not pend.get("active") or pend.get("slot") != slot:
            with contextlib.suppress(Exception):
                await callback.answer("Замена уже закрыта.", show_alert=False)
            return

        deal = await get_deal_by_id(int(deal_id)) or {}
        game = str(deal.get("game_name") or deal.get("name") or "")

        # «Светофор»
        role = _role_from_slot(slot)
        status = ""
        with contextlib.suppress(Exception):
            status = await get_user_status_from_svetofor(uid, game)
        ok = (status == "green") if role == "main" else (status in {"green", "yellow"})
        if not ok:
            with contextlib.suppress(Exception):
                await callback.answer("Недостаточный статус по Светофору.", show_alert=True)
            return

        # Локальная фиксация: ставим человека в слот
        locked = (getattr(state, "locked_distribution", {}) or {})
        dist = locked.get(int(deal_id)) or locked.get(str(deal_id)) or {}
        if not isinstance(dist, dict):
            dist = {}
        label = await short_name(uid)
        dist[slot] = f"{label}|{uid}"
        locked[int(deal_id)] = dist
        setattr(state, "locked_distribution", locked)

        # Тег в AmoCRM (добавочный)
        suf = {"main": "1", "assist": "2", "admin": "Адм"}.get(role, "2")
        tag = f"{label}.{suf}"
        with contextlib.suppress(Exception):
            await update_amocrm_tags({str(int(deal_id)): {slot: tag}})

        # Закрываем запрос замены
        pend["active"] = False
        getattr(state, "pending_swaps", {})[int(deal_id)] = pend

        # Уведомление «Состав обновлён… Подтвердите участие…»
        lines = await team_bulleted_lines(dist)
        text = "✅ Состав команды обновлён.\n" + "\n".join(lines) + "\n\nПодтвердите участие в личном кабинете."
        with contextlib.suppress(Exception):
            chat_id = resolve_notify_chat_id()
            if isinstance(chat_id, int):
                bot = Bot.get_current()
                await bot.send_message(chat_id, text, disable_web_page_preview=True)

        with contextlib.suppress(Exception):
            await callback.answer("Вы назначены. Спасибо!")


# ── Экспорт ─────────────────────────────────────────────────────────
__all__ = ["router", "mygames_swap_request", "mygames_swap_accept"]


# ── Мини-тесты ─────────────────────────────────────────────────────
def _test() -> None:
    assert _parse_req("mygames_swap_12345") == 12345
    assert _parse_req("mygames_swap_0") == 0  # допустимы «0» в тестах
    assert _parse_req("swap_accept_1_x") is None
    assert _parse_acc("mygames_swap_accept_10_lead1") == (10, "lead1")
    assert _role_from_slot("lead1") == "main"
    assert _role_from_slot("assistant2") == "assist"
    assert _role_from_slot("admin") == "admin"

if __name__ == "__main__":
    _test()
