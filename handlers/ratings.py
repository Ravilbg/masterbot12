from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.utils import dm_singleton_edit_or_send
from core.state import state
from services.ratings import get_user_stats

router = Router(name="ratings_user")

BUTTON_MY_RATING = "⭐ Мой рейтинг"

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


def _format_events(events: List[Dict[str, Any]], limit: int = 5) -> str:
    if not events:
        return "• Нет событий за период"
    def _human_event_label(kind: str, meta: Optional[Dict[str, Any]] = None) -> str:
        meta = meta or {}
        mapping: Dict[str, str] = {
            "poll_reply_d12": "📨 Отклик ≤12ч",
            "poll_reply_d36": "📨 Отклик ≤36ч",
            "poll_reply_late": "📨 Отклик (поздно)",
            "confirm_d24": "✅ Подтверждение ≤24ч",
            "confirm_d36": "✅ Подтверждение ≤36ч",
            "confirm_late": "✅ Подтверждение (поздно)",
            "game_main": "🎭 Вёл игру",
            "game_assist": "🎭 Помогал в игре",
            "game_admin": "🛡️ Администратор",
            "game_trainee": "🧑‍💼 Стажёр",
            "learn_game": "📚 Обучение/репетиция",
            "urgent_replacement": "⚡ Срочная замена",
            "manual_adjust": "🔧 Ручная поправка",
            "bonus_hero": "🏅 Бонус за подвиг",
            "miss_meeting": "🚫 Пропуск собрания",
            "late": "⏰ Опаздание",
            "poor_review": "⚠️ Некачественный разбор",
            "attend_meeting": "✅ Выход на собрание",
            "no_reply_penalty_w1": "⚠️ Не ответил (штраф)",
            "no_reply_penalty_w2": "⚠️ Не ответил (повторный штраф)",
            "cant_work_2w_row": "🚫 Не могу (2 недели подряд)",
            "cant_work_3w_row": "🚫 Не могу (3 недели подряд)",
        }
        base = mapping.get(kind, kind)
        if isinstance(meta, dict) and meta.get("delta_hours") is not None:
            try:
                dh = float(meta.get("delta_hours"))
                base = f"{base} ({dh:.1f}ч)"
            except Exception:
                pass
        return base

    lines: List[str] = []
    for item in events[:limit]:
        when_ts = int(item.get("when_ts", 0))
        when = datetime.fromtimestamp(when_ts).strftime("%d.%m %H:%M") if when_ts else "—"
        kind = str(item.get("kind", ""))
        points = int(item.get("points", 0))
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        suffix = ""
        if isinstance(meta, dict):
            if meta.get("poll_id"):
                suffix = f" (опрос {meta['poll_id']})"
            elif meta.get("deal_id"):
                suffix = f" (сделка {meta['deal_id']})"
        label = _human_event_label(kind, meta)
        if kind == "manual_adjust" and isinstance(meta, dict):
            try:
                delta = int(meta.get("delta", points))
            except Exception:
                delta = int(points)
            reason = str(meta.get("reason") or "").strip()
            reason_s = f" — {reason}" if reason else ""
            lines.append(f"• {when}: {label} {delta:+d}{reason_s}{suffix}")
        else:
            lines.append(f"• {when}: {label} {points:+d}{suffix}")
    return "\n".join(lines)


def _rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Обновить", callback_data="ratings_show_my")]]
    )


async def _render_user_rating(uid: int, message_id: Optional[int]) -> None:
    # При показе «Мой рейтинг» — не сохранять отчётный дашборд лидера
    try:
        setattr(state, "suppress_report_keep", True)
    except Exception:
        pass
    stats = await get_user_stats(uid)
    score = int(stats.get("score", 0))
    baseline = int(stats.get("baseline", 0))
    manual = int(stats.get("manual_delta", 0))
    groups = stats.get("groups", {})
    events = stats.get("events", [])
    window_days = int(stats.get("window_days", 30))
    try:
        manual_val = int(stats.get("manual_delta", 0))
    except Exception:
        manual_val = 0
    groups_display = dict(groups or {})
    if manual_val:
        groups_display["manual"] = groups_display.get("manual", 0) + int(manual_val)
    text = (
        f"⭐ Текущий рейтинг: <b>{score}</b> / 100\n\n"
        f"База (Светофор): {baseline}\n"
        f"События за {window_days} дн.:\n{_format_groups(groups_display)}\n\n"
        "Последние события:\n"
        f"{_format_events(events)}\n\n"
        "Совет: откликайтесь ≤ 12 часов (+2), подтверждайте ≤ 24 часов (+2),\n"
        "избегайте молчания более 48 часов (−20/−30)."
    )

    await dm_singleton_edit_or_send(
        uid,
        message_id,
        text,
        parse_mode="HTML",
        reply_markup=_rating_keyboard(),
    )
    # вернуть флаг
    try:
        setattr(state, "suppress_report_keep", False)
    except Exception:
        pass


@router.message(F.text == BUTTON_MY_RATING)
async def ratings_entry_message(message: types.Message) -> None:
    await _render_user_rating(message.from_user.id, None)


@router.callback_query(F.data == "ratings_show_my")
async def ratings_entry_callback(callback: types.CallbackQuery) -> None:
    await callback.answer()
    msg_id = getattr(callback.message, "message_id", None)
    await _render_user_rating(callback.from_user.id, msg_id)


# 2025-09-17 · модуль рейтинга: выровнено под SSOT.
