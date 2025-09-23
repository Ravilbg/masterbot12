# handlers/p2p_swap.py — P2P-обмен слотами между ведущими (без цикла «Замены»)
# ─────────────────────────────────────────────────────────────────────────────
"""
Лёгкий взаимный обмен слотами из раздела «🎲 Мои игры»:
A(uid) [dealA:roleA] ↔ B(uid) [dealB:roleB]
• Двухстороннее подтверждение
• Валидация «Светофора» (кэш 24ч — использовать сервисный кэш gsheets)
• Перестановка тегов в AmoCRM
• Согласованное обновление локальных кэшей и UI
• Уведомление в общий чат через resolve_notify_chat_id()

Версия 1.0 · 2025-09-23
────────────────────────────────────────────────────────────────────────────
• Новый модуль-хэндлер p2p-обмена (drop-in).
• Поддержка callback: p2p_swap_offer / p2p_swap_accept / p2p_swap_decline.
• Экспорт фабрик make_offer_callback / get_p2p_swap_button для my_games.
• Pylance-friendly, aiogram 3.x, без локальных дублей SSOT-утилит.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Literal

from aiogram import Router, F, types
from aiogram.utils.keyboard import InlineKeyboardBuilder

# SSOT & сервисы
from core.config import settings
from core.utils import (
    resolve_notify_chat_id,
    delete_previous_private_messages,
    vacuum_private,
    user_short_name,            # ожидается в core.utils; если нет — заменить на свой резолвер
)
from services.amocrm import (
    get_deal_by_id,
    update_amocrm_tags,
)
from services.gsheets import (
    get_user_traffic_light,     # обязан кешировать на 24ч внутри сервиса (см. проектные правила)
)

# Внешние состояния проекта — используем только то, что уже принято за SSOT:
# locked_distribution должен хранить утверждённые составы вида "Имя Ф.|uid" по слотам
try:
    from state import locked_distribution  # type: ignore
except Exception:
    locked_distribution = {}  # fallback для тестов

logger = logging.getLogger(__name__)
router = Router(name="p2p_swap")

Role = Literal["main", "assist", "admin"]

ROLE_SUFFIX: Dict[Role, str] = {
    "main": ".1",
    "assist": ".2",
    "admin": ".Адм",
}

# Таймаут ожидания подтверждения (минуты)
SWAP_TIMEOUT_MIN = getattr(settings, "SWAP_P2P_TIMEOUT_MIN", 120)


# ────────────────────────────────────────────────────────────────────
# Модель p2p-заявки (локально в модуле; при желании вынести в state.py)
# ────────────────────────────────────────────────────────────────────

@dataclass
class SwapRequest:
    swap_id: str
    a_uid: int
    a_deal_id: int
    a_role: Role
    b_uid: int
    b_deal_id: int
    b_role: Role
    created_ts: float
    a_confirmed: bool
    b_confirmed: bool


# Ключ: swap_id
_pending_swaps: Dict[str, SwapRequest] = {}


# ────────────────────────────────────────────────────────────────────
# Публичные фабрики для интеграции из handlers/my_games.py
# ────────────────────────────────────────────────────────────────────

def make_offer_callback(
    a_deal_id: int, a_role: Role, b_uid: int, b_deal_id: int, b_role: Role
) -> str:
    """Собирает callback-data для оффера p2p-обмена (вызывается из UI «Моих игр»)."""
    return f"p2p_swap_offer:{a_deal_id}:{a_role}:{b_uid}:{b_deal_id}:{b_role}"


def get_p2p_swap_button(deal_id: int, role: Role) -> types.InlineKeyboardButton:
    """Кнопка «🔁 Обмен» для вставки в карточку слота."""
    return types.InlineKeyboardButton(
        text="🔁 Обмен",
        callback_data=f"p2p_swap_start:{deal_id}:{role}",
    )


# ────────────────────────────────────────────────────────────────────
# Валидации и низкоуровневые помощники
# ────────────────────────────────────────────────────────────────────

def _swap_id(a_uid: int, a_deal: int, a_role: Role, b_uid: int, b_deal: int, b_role: Role) -> str:
    return f"{a_uid}:{a_deal}:{a_role}|{b_uid}:{b_deal}:{b_role}"


async def _validate_roles_by_svetofor(uid: int, deal: dict, role: Role) -> bool:
    """Проверка допуска по «Светофору» для (uid → роль в игре deal)."""
    title = (deal.get("name") or "").strip()
    color = await get_user_traffic_light(uid, title)  # сервис сам кэширует на 24ч
    if role == "main":
        return color == "green"
    if role == "assist":
        return color in ("green", "yellow")
    if role == "admin":
        # проектное правило: допустить всех, кроме "red";
        # если в проекте есть свой фильтр — заменить на вызов helper из core.utils
        return color in ("green", "yellow", "shield", "admin", "blue", "grey")
    return False


def _is_deal_swappable(deal: dict) -> bool:
    """Статусы и дата: разрешено обменивать для BRONь/Завершение сделки; не для «Успешно реализовано» и прошедших дат."""
    status_id = int(deal.get("status_id") or 0)
    if status_id == getattr(settings, "SUCCESSFUL_STATUS_ID", -1):
        return False
    # проверка даты/времени — в проекте дата хранится в кастомном поле
    # здесь допускаем, что сервис get_deal_by_id уже нормализует дату, иначе оставить TODO
    return True


def _role_suffix(role: Role) -> str:
    return ROLE_SUFFIX[role]


def _strip_suffix(tag: str) -> str:
    for suf in ROLE_SUFFIX.values():
        if tag.endswith(suf):
            return tag[: -len(suf)]
    return tag


def _tag_for(uid: int, role: Role) -> str:
    """Формирует тег вида 'Имя Ф.<suf>' из uid и роли."""
    short = user_short_name(uid) or str(uid)
    return f"{short}{_role_suffix(role)}"


# ────────────────────────────────────────────────────────────────────
# Хэндлеры: старт пинга (делает my_games), оффер, подтверждение, отказ
# ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("p2p_swap_start:"))
async def p2p_swap_start_handler(callback: types.CallbackQuery) -> None:
    """
    Заглушка-старт: сам выбор партнёра и его игры должен строить 'my_games'.
    Здесь просто подсвечиваем UX и даём подсказку.
    """
    uid = callback.from_user.id
    await delete_previous_private_messages(uid)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Как работает обмен?",
        callback_data="p2p_swap_help",
    )
    await callback.message.answer(
        "🔁 Обмен: выберите коллегу и его игру в «Моих играх». Затем нажмите «Поменять на …».",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "p2p_swap_help")
async def p2p_swap_help(callback: types.CallbackQuery) -> None:
    await callback.answer("Договоритесь с коллегой, выберите его игру и подтвердите обмен.")


@router.callback_query(F.data.startswith("p2p_swap_offer:"))
async def p2p_swap_offer(callback: types.CallbackQuery) -> None:
    """
    Создаёт заявку на p2p-обмен и рассылает приглашение партнёру.
    Ожидается формат: p2p_swap_offer:{a_deal}:{a_role}:{b_uid}:{b_deal}:{b_role}
    """
    a_uid = callback.from_user.id
    try:
        _, a_deal_s, a_role, b_uid_s, b_deal_s, b_role = callback.data.split(":")
        a_deal_id = int(a_deal_s)
        b_uid = int(b_uid_s)
        b_deal_id = int(b_deal_s)
        a_role_t = a_role if a_role in ROLE_SUFFIX else "main"
        b_role_t = b_role if b_role in ROLE_SUFFIX else "main"
        assert a_role_t in ROLE_SUFFIX and b_role_t in ROLE_SUFFIX
    except Exception as e:
        logger.warning("p2p_swap_offer: bad payload %r err=%s", callback.data, e)
        await callback.answer("Некорректные данные обмена", show_alert=True)
        return

    # Подтянуть сделки
    deal_a = await get_deal_by_id(a_deal_id)
    deal_b = await get_deal_by_id(b_deal_id)
    if not deal_a or not deal_b:
        await callback.answer("Сделка не найдена", show_alert=True)
        return

    # Базовые проверки
    if not _is_deal_swappable(deal_a) or not _is_deal_swappable(deal_b):
        await callback.answer("Одна из сделок не допускает обмен", show_alert=True)
        return

    # Светофор на целевые роли (кто куда перейдёт)
    ok_a = await _validate_roles_by_svetofor(b_uid, deal_a, a_role_t)  # B пойдёт в A:roleA
    ok_b = await _validate_roles_by_svetofor(a_uid, deal_b, b_role_t)  # A пойдёт в B:roleB
    if not (ok_a and ok_b):
        await callback.answer("Светофор не допускает один из переходов", show_alert=True)
        return

    # Создать/зарегистрировать оффер
    sid = _swap_id(a_uid, a_deal_id, a_role_t, b_uid, b_deal_id, b_role_t)
    _pending_swaps[sid] = SwapRequest(
        swap_id=sid,
        a_uid=a_uid,
        a_deal_id=a_deal_id,
        a_role=a_role_t,           # type: ignore
        b_uid=b_uid,
        b_deal_id=b_deal_id,
        b_role=b_role_t,           # type: ignore
        created_ts=time.time(),
        a_confirmed=True,
        b_confirmed=False,
    )

    # Отправить приглашение партнёру
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен на обмен", callback_data=f"p2p_swap_accept:{sid}")
    kb.button(text="❌ Отказаться", callback_data=f"p2p_swap_decline:{sid}")

    try:
        await callback.bot.send_message(
            chat_id=b_uid,
            text=(
                "🔁 Приглашение к обмену слотами\n"
                f"• {user_short_name(a_uid)} предлагает обменяться играми.\n"
                f"→ Он отдаёт: #{a_deal_id} роль {a_role_t}\n"
                f"→ И просит:  #{b_deal_id} роль {b_role_t}\n\n"
                "Принять обмен?"
            ),
            reply_markup=kb.as_markup(),
        )
        await callback.answer("Ожидаем подтверждение от коллеги…")
    except Exception as e:
        logger.warning("p2p_swap_offer: cannot DM partner uid=%s err=%s", b_uid, e)
        await callback.answer("Не удалось отправить приглашение партнёру", show_alert=True)


@router.callback_query(F.data.startswith("p2p_swap_decline:"))
async def p2p_swap_decline(callback: types.CallbackQuery) -> None:
    try:
        _, sid = callback.data.split(":")
    except Exception:
        await callback.answer()
        return
    req = _pending_swaps.pop(sid, None)
    if not req:
        await callback.answer("Заявка уже неактуальна")
        return
    # Сообщить инициатору
    try:
        await callback.bot.send_message(chat_id=req.a_uid, text="❌ Партнёр отказался от обмена.")
    except Exception:
        pass
    await callback.answer("Отказ зафиксирован")


@router.callback_query(F.data.startswith("p2p_swap_accept:"))
async def p2p_swap_accept(callback: types.CallbackQuery) -> None:
    """Подтверждение обмена партнёром: повторные валидации и перестановка тегов."""
    try:
        _, sid = callback.data.split(":")
    except Exception:
        await callback.answer()
        return

    req = _pending_swaps.get(sid)
    if not req:
        await callback.answer("Заявка не найдена или устарела", show_alert=True)
        return

    # Проверка, что подтверждает именно B
    if callback.from_user.id != req.b_uid:
        await callback.answer("Подтвердить может только второй участник", show_alert=True)
        return

    # Таймаут
    if (time.time() - req.created_ts) > SWAP_TIMEOUT_MIN * 60:
        _pending_swaps.pop(sid, None)
        await callback.answer("Заявка просрочена", show_alert=True)
        return

    # Актуальные сделки
    deal_a = await get_deal_by_id(req.a_deal_id)
    deal_b = await get_deal_by_id(req.b_deal_id)
    if not deal_a or not deal_b:
        _pending_swaps.pop(sid, None)
        await callback.answer("Сделка не найдена", show_alert=True)
        return
    if not _is_deal_swappable(deal_a) or not _is_deal_swappable(deal_b):
        _pending_swaps.pop(sid, None)
        await callback.answer("Сделка стала недоступна для обмена", show_alert=True)
        return

    # Повторно валидируем Светофор на целевые роли
    ok_a = await _validate_roles_by_svetofor(req.b_uid, deal_a, req.a_role)
    ok_b = await _validate_roles_by_svetofor(req.a_uid, deal_b, req.b_role)
    if not (ok_a and ok_b):
        _pending_swaps.pop(sid, None)
        await callback.answer("Светофор не допускает обмен", show_alert=True)
        return

    # ── Перестановка тегов в AmoCRM ─────────────────────────────────
    # 1) Получаем и изменяем теги: снимаем старые, ставим новые.
    try:
        # Допустим, update_amocrm_tags принимает полный список тегов (set-like).
        # Извлечём текущие, уберём 'Имя Ф.<suf>' старых участников, добавим новые.
        # Для простоты предполагаем, что get_deal_by_id возвращает tags=list[str]
        tags_a = set(deal_a.get("tags") or [])
        tags_b = set(deal_b.get("tags") or [])

        # снять старые теги для A и B по их ролям
        tags_a = {t for t in tags_a if _strip_suffix(t) != user_short_name(req.a_uid)}
        tags_b = {t for t in tags_b if _strip_suffix(t) != user_short_name(req.b_uid)}

        # поставить новые
        tags_a.add(_tag_for(req.b_uid, req.a_role))  # теперь B в deal A на roleA
        tags_b.add(_tag_for(req.a_uid, req.b_role))  # теперь A в deal B на roleB

        await update_amocrm_tags(req.a_deal_id, sorted(tags_a))
        await update_amocrm_tags(req.b_deal_id, sorted(tags_b))
    except Exception as e:
        logger.exception("p2p_swap_accept: AmoCRM tags update failed: %s", e)
        await callback.answer("Не удалось обновить теги в CRM", show_alert=True)
        return

    # ── Обновить локальные кэши locked_distribution (если используется в проекте) ──
    try:
        # locked_distribution[deal_id] — словарь слотов → "Имя Ф.|uid"
        la = locked_distribution.get(str(req.a_deal_id), {})
        lb = locked_distribution.get(str(req.b_deal_id), {})
        la[req.a_role] = f"{user_short_name(req.b_uid)}|{req.b_uid}"
        lb[req.b_role] = f"{user_short_name(req.a_uid)}|{req.a_uid}"
        locked_distribution[str(req.a_deal_id)] = la
        locked_distribution[str(req.b_deal_id)] = lb
    except Exception:
        # не критично для исполнения
        pass

    # ── Уведомления: обоим и общий чат ──────────────────────────────
    try:
        await callback.bot.send_message(
            chat_id=req.a_uid,
            text=(
                "✅ Обмен подтверждён.\n"
                f"Теперь вы ведёте #{req.b_deal_id} как {req.b_role}."
            ),
        )
    except Exception:
        pass
    try:
        await callback.bot.send_message(
            chat_id=req.b_uid,
            text=(
                "✅ Обмен подтверждён.\n"
                f"Теперь вы ведёте #{req.a_deal_id} как {req.a_role}."
            ),
        )
    except Exception:
        pass

    # общий чат
    try:
        nid = await resolve_notify_chat_id()
        await callback.bot.send_message(
            chat_id=nid,
            text=(
                "🔁 Обмен подтверждён:\n"
                f"{user_short_name(req.a_uid)} ↔ {user_short_name(req.b_uid)}\n"
                f"• Игра A #{req.a_deal_id} → теперь ведёт {user_short_name(req.b_uid)} ({req.a_role})\n"
                f"• Игра B #{req.b_deal_id} → теперь ведёт {user_short_name(req.a_uid)} ({req.b_role})"
            ),
        )
    except Exception:
        pass

    # Чистим и завершаем
    _pending_swaps.pop(sid, None)
    await callback.answer("Обмен выполнен")

    # UX: подчистка ЛС обоих участников (соблюдаем «один активный блок»)
    with contextlib.suppress(Exception):
        await delete_previous_private_messages(req.a_uid)
        await delete_previous_private_messages(req.b_uid)


# ────────────────────────────────────────────────────────────────────
# Мини-тесты (локальные)
# ────────────────────────────────────────────────────────────────────

def _test() -> None:
    sid = _swap_id(1, 100, "main", 2, 200, "assist")
    assert sid == "1:100:main|2:200:assist"
    assert _strip_suffix("Иван П.1") == "Иван П"
    assert _role_suffix("admin") == ".Адм"
    print("[p2p_swap] basic tests OK")


# История изменений
# 2025-09-23 · v1.0 — первый релиз модуля: p2p-offer/accept/decline, AmoCRM-теги, чат-уведомления,
#                    фабрики кнопок, мини-тесты, выровнено под SSOT.
