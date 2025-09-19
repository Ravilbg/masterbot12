from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import settings
from core.db import get_all_leader_uids
from core.utils import dm_singleton_edit_or_send, short_name
from services.ratings import adjust_score, get_cant_work_weeks, get_scores, get_user_stats

router = Router(name="ratings_admin")

BUTTON_TEAM_RATING = "⭐ Рейтинг команды"

_GROUP_TITLES: Dict[str, str] = {
    "poll_reply": "Отклики",
    "confirm": "Подтверждения",
    "games": "Игры",
    "learning": "Обучение",
    "urgent": "Срочные замены",
    "penalties": "Штрафы",
    "manual": "Ручная поправка",
    "other": "Прочее",
}


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _cant_chain_label(weeks: List[str]) -> str:
    if not weeks:
        return "нет"
    weeks_set = {str(w) for w in weeks}
    now = datetime.now(tz=datetime.now().astimezone().tzinfo)
    w0, w1, w2 = _week_key(now), _week_key(now - timedelta(weeks=1)), _week_key(now - timedelta(weeks=2))
    if w0 in weeks_set and w1 in weeks_set and w2 in weeks_set:
        return "3 недели подряд"
    if w0 in weeks_set and w1 in weeks_set:
        return "2 недели подряд"
    return "есть отметка"


async def _sorted_scores() -> List[Tuple[int, int]]:
    uids = [int(uid) for uid in await get_all_leader_uids()]
    scores = await get_scores(uids) if uids else {}
    return sorted(((uid, int(scores.get(uid, 0))) for uid in uids), key=lambda item: item[1], reverse=True)


def _format_groups(groups: Dict[str, int]) -> str:
    parts: List[str] = []
    for key, title in _GROUP_TITLES.items():
        if key not in groups:
            continue
        value = int(groups.get(key, 0))
        if value:
            parts.append(f"• {title}: {value:+d}")
    if not parts:
        return "• Изменений за период нет"
    return "\n".join(parts)


def _format_events(events: List[Dict[str, int]], limit: int = 5) -> str:
    if not events:
        return "• Нет событий за период"
    rows: List[str] = []
    for item in events[:limit]:
        when_ts = int(item.get("when_ts", 0))
        when = datetime.fromtimestamp(when_ts).strftime("%d.%m %H:%M") if when_ts else "—"
        kind = str(item.get("kind", ""))
        points = int(item.get("points", 0))
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        suffix = ""
        if isinstance(meta, dict):
            if meta.get("deal_id"):
                suffix = f" (сделка {meta['deal_id']})"
            elif meta.get("poll_id"):
                suffix = f" (опрос {meta['poll_id']})"
        rows.append(f"• {when}: {kind} {points:+d}{suffix}")
    return "\n".join(rows)


def _dashboard_keyboard(rows: List[Tuple[int, int]]) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []
    for index, (uid, _) in enumerate(rows, start=1):
        keyboard.append([
            InlineKeyboardButton(text=f"{index}.", callback_data=f"ratings_admin_user_{uid}")
        ])
    keyboard.append([InlineKeyboardButton(text="Обновить", callback_data="ratings_admin_dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _user_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+1", callback_data=f"ratings_admin_adjust_{uid}_1"),
                InlineKeyboardButton(text="−1", callback_data=f"ratings_admin_adjust_{uid}_-1"),
            ],
            [
                InlineKeyboardButton(text="+5", callback_data=f"ratings_admin_adjust_{uid}_5"),
                InlineKeyboardButton(text="−5", callback_data=f"ratings_admin_adjust_{uid}_-5"),
            ],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="ratings_admin_dashboard")],
        ]
    )


def _is_leader(user_id: int) -> bool:
    return int(user_id) == int(getattr(settings, "LEADER_ID", 0))


async def _render_dashboard(admin_uid: int, message_id: Optional[int]) -> None:
    rows = await _sorted_scores()
    body: List[str] = ["⭐ Рейтинг команды"]
    for index, (member_id, score) in enumerate(rows, start=1):
        name = await short_name(member_id)
        body.append(f"{index}. {name} — {score}")
    if not rows:
        body.append("Нет активных пользователей.")
    await dm_singleton_edit_or_send(
        admin_uid,
        message_id,
        "\n".join(body),
        reply_markup=_dashboard_keyboard(rows),
    )


async def _render_user_detail(admin_uid: int, target_uid: int, message_id: Optional[int]) -> None:
    stats = await get_user_stats(target_uid)
    name = await short_name(target_uid)
    score = int(stats.get("score", 0))
    baseline = int(stats.get("baseline", 0))
    manual = int(stats.get("manual_delta", 0))
    groups = stats.get("groups", {})
    events = stats.get("events", [])

    weeks_map = await get_cant_work_weeks(target_uid)
    weeks_list = weeks_map.get(target_uid, []) if isinstance(weeks_map, dict) else []

    text = (
        f"⭐ {name} — <b>{score}</b> / 100\n\n"
        f"База (Светофор): {baseline}\n"
        f"Ручная поправка: {manual:+d}\n"
        f"Цепочка «не могу»: {_cant_chain_label(weeks_list)}\n\n"
        f"Разбор событий:\n{_format_groups(groups)}\n\n"
        "Последние события:\n"
        f"{_format_events(events)}"
    )

    await dm_singleton_edit_or_send(
        admin_uid,
        message_id,
        text,
        parse_mode="HTML",
        reply_markup=_user_keyboard(target_uid),
    )


@router.message(F.text == BUTTON_TEAM_RATING)
async def ratings_admin_message(message: types.Message) -> None:
    if not _is_leader(message.from_user.id):
        await message.answer("Недоступно.")
        return
    await _render_dashboard(message.from_user.id, None)


@router.callback_query(F.data == "ratings_admin_dashboard")
async def ratings_admin_dashboard(callback: types.CallbackQuery) -> None:
    if not _is_leader(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer()
    await _render_dashboard(callback.from_user.id, getattr(callback.message, "message_id", None))


@router.callback_query(F.data.startswith("ratings_admin_user_"))
async def ratings_admin_user(callback: types.CallbackQuery) -> None:
    if not _is_leader(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        target_uid = int(callback.data.rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Ошибка", show_alert=True)
        return
    await callback.answer()
    await _render_user_detail(callback.from_user.id, target_uid, getattr(callback.message, "message_id", None))


@router.callback_query(F.data.startswith("ratings_admin_adjust_"))
async def ratings_admin_adjust(callback: types.CallbackQuery) -> None:
    if not _is_leader(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        _, _, _, uid_str, delta_str = callback.data.split("_", 4)
        target_uid = int(uid_str)
        delta = int(delta_str)
    except Exception:
        await callback.answer("Ошибка", show_alert=True)
        return
    try:
        await adjust_score(target_uid, delta, reason=f"admin:{callback.from_user.id}:{delta}")
        await callback.answer("Сохранено")
    except Exception as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)
        return
    await _render_user_detail(callback.from_user.id, target_uid, getattr(callback.message, "message_id", None))


# 2025-09-17 · модуль рейтинга: выровнено под SSOT.
