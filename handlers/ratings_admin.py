from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import settings
from core.db import get_all_leader_uids
from core.utils import dm_singleton_edit_or_send, short_name, delete_previous_private_messages
try:
    from core.menu import get_menu_message_id, get_main_menu, send_root_menu_singleton  # type: ignore
except Exception:
    def get_menu_message_id(uid: int) -> Optional[int]:
        return None

    async def get_main_menu(uid: int) -> Optional[Any]:  # type: ignore[override]
        return None

    async def send_root_menu_singleton(uid: int, kb: Any, *, pin: bool = False) -> int:  # type: ignore[override]
        return 0
from services.ratings import adjust_score, get_cant_work_weeks, get_scores, get_user_stats
from core.state import state
import logging

logger = logging.getLogger(__name__)

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

# Кнопки причин для руководителя: (code, label, delta)
_LEADER_REASONS: List[Tuple[str, str, int]] = [
    ("bonus_hero", "Бонус за подвиг +5", 5),
    ("miss_meeting", "Пропуск собрания -10", -10),
    ("late", "Опаздание -5", -5),
    ("poor_review", "Некачесвеный разбор игры -5", -5),
    ("attend_meeting", "Выход на собрание +10", 10),
]


def _log_menu_state(uid: int, stage: str) -> None:
    """Диагностика: фиксируем состояние меню/стикеров перед/после пылесоса."""
    try:
        try:
            menu_mid = get_menu_message_id(int(uid))
        except Exception:
            menu_mid = None
        state_menu = getattr(state, "menu_message_id", None)
        sticky = getattr(state, "my_games_sticky", None)
        legacy_sticky = getattr(state, "my_games_dashboard", None)
        logger.info(
            "[ratings_admin] %s uid=%s menu_mid=%r state_menu=%r sticky=%r legacy_sticky=%r",
            stage,
            int(uid),
            menu_mid,
            state_menu,
            sticky,
            legacy_sticky,
        )
    except Exception:
        pass


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
    # add small meta hints
    if isinstance(meta, dict):
        if meta.get("delta_hours") is not None:
            try:
                dh = float(meta.get("delta_hours"))
                base = f"{base} ({dh:.1f}ч)"
            except Exception:
                pass
    return base


def _format_events(events: List[Dict[str, Any]], limit: int = 5) -> str:
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
        label = _human_event_label(kind, meta)
        # special-case manual_adjust: show delta and reason from meta
        if kind == "manual_adjust" and isinstance(meta, dict):
            try:
                delta = int(meta.get("delta", points))
            except Exception:
                delta = int(points)
            reason = str(meta.get("reason") or "").strip()
            reason_s = f" — {reason}" if reason else ""
            rows.append(f"• {when}: {label} {delta:+d}{reason_s}{suffix}")
        else:
            rows.append(f"• {when}: {label} {points:+d}{suffix}")
    return "\n".join(rows)


async def _dashboard_keyboard(rows: List[Tuple[int, int]]) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []
    for index, (uid, score) in enumerate(rows, start=1):
        try:
            name = await short_name(uid)
        except Exception:
            name = f"uid:{uid}"
        label = f"{name} — {int(score)}"
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"ratings_admin_user_{uid}")])
    keyboard.append([InlineKeyboardButton(text="Обновить", callback_data="ratings_admin_dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _user_keyboard(uid: int) -> InlineKeyboardMarkup:
    # Для руководителя — кнопки с причинами/событиями
    rows: List[List[InlineKeyboardButton]] = []
    for code, label, delta in _LEADER_REASONS:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"ratings_admin_apply_{uid}_{code}")])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="ratings_admin_dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_leader(user_id: int) -> bool:
    return int(user_id) == int(getattr(settings, "LEADER_ID", 0))


async def _render_dashboard(admin_uid: int, message_id: Optional[int]) -> None:
    rows = await _sorted_scores()
    body: List[str] = ["⭐ Рейтинг команды\n(нажмите на имя, чтобы открыть детали)"]
    if not rows:
        body.append("Нет активных пользователей.")

    # Очистим старые ЛС-сообщения (например, старый список команды), но НЕ будем удалять главное меню
    try:
        _log_menu_state(admin_uid, "pre-vacuum:enter")
        # Не сохранять дашборд отчёта лидера при входе в рейтинг — он должен удаляться
        try:
            setattr(state, "suppress_report_keep", True)
        except Exception:
            pass
        # Перед пылесосом — уберём sticky «Мои игры», чтобы он НЕ сохранялся
        # (иначе keep_for_vacuum добавит его автоматически и дашборд останется)
        try:
            d = getattr(state, "my_games_sticky", None)
            if isinstance(d, dict):
                d.pop(int(admin_uid), None)
        except Exception:
            pass
        # Поддержка старого места хранения sticky (для совместимости)
        try:
            legacy = getattr(state, "my_games_dashboard", None)
            if isinstance(legacy, dict):
                legacy.pop(int(admin_uid), None)
        except Exception:
            pass

        menu_mid = None
        try:
            menu_mid = get_menu_message_id(int(admin_uid))
        except Exception:
            # fallback to local importer if present
            try:
                from core.menu import get_menu_message_id as _get_menu_mid
                menu_mid = _get_menu_mid(int(admin_uid))
            except Exception:
                menu_mid = None
        keep = [int(menu_mid)] if isinstance(menu_mid, int) and menu_mid else []
        logger.info("[ratings_admin] pre-vacuum keep=%r (menu_mid=%r)", keep, menu_mid)
        try:
            await delete_previous_private_messages(admin_uid, keep=keep)
        except TypeError:
            try:
                from aiogram import Bot
                await delete_previous_private_messages(Bot.get_current(), admin_uid, keep=keep)
            except Exception:
                pass
        _log_menu_state(admin_uid, "post-vacuum:after_delete")
        # ensure menu still exists; if vacuum removed it, restore
        try:
            post_menu = get_menu_message_id(int(admin_uid))
            if not post_menu:
                kb = await get_main_menu(int(admin_uid))
                if kb is not None:
                    try:
                        mid = await send_root_menu_singleton(int(admin_uid), kb)
                        logger.warning("[ratings_admin] menu restored via send_root_menu_singleton uid=%s mid=%s", int(admin_uid), mid)
                        # критично: синхронизируем state.menu_message_id, чтобы следующий strict_vacuum сохранил меню
                        try:
                            mm = getattr(state, "menu_message_id", None)
                            if isinstance(mm, dict):
                                mm[int(admin_uid)] = int(mid)
                            elif isinstance(mm, int):
                                setattr(state, "menu_message_id", int(mid))
                            else:
                                setattr(state, "menu_message_id", {int(admin_uid): int(mid)})
                            logger.info("[ratings_admin] state.menu_message_id updated for uid=%s -> %s", int(admin_uid), int(mid))
                        except Exception:
                            pass
                    except Exception:
                        pass
            else:
                # меню есть — убедимся, что state.menu_message_id его знает
                try:
                    mm = getattr(state, "menu_message_id", None)
                    if isinstance(mm, dict):
                        mm[int(admin_uid)] = int(post_menu)
                    elif isinstance(mm, int):
                        setattr(state, "menu_message_id", int(post_menu))
                    else:
                        setattr(state, "menu_message_id", {int(admin_uid): int(post_menu)})
                    logger.info("[ratings_admin] state.menu_message_id ensured for uid=%s -> %s", int(admin_uid), int(post_menu))
                except Exception:
                    pass
        except Exception:
            pass
        _log_menu_state(admin_uid, "post-vacuum:after_ensure")
    except Exception:
        # best-effort cleanup failed — ничего страшного
        pass
    finally:
        # вернуть флаг подавления, чтобы не затронуть другие экраны
        try:
            setattr(state, "suppress_report_keep", False)
        except Exception:
            pass

    kb = await _dashboard_keyboard(rows)
    # Prefer editing existing personal report message (if leader has one) to avoid moving it up
    msg_id_to_use = message_id
    try:
        cur_leader = getattr(state, "current_poll_leader", None)
        report_mid = getattr(state, "personal_report_message_id", None)
        if (not msg_id_to_use) and isinstance(cur_leader, int) and int(cur_leader) == int(admin_uid) and isinstance(report_mid, int) and report_mid > 0:
            msg_id_to_use = int(report_mid)
    except Exception:
        pass
    _log_menu_state(admin_uid, "before-dm_singleton:dashboard")
    await dm_singleton_edit_or_send(
        admin_uid,
        msg_id_to_use,
        "\n".join(body),
        reply_markup=kb,
    )
    _log_menu_state(admin_uid, "after-dm_singleton:dashboard")


async def _render_user_detail(admin_uid: int, target_uid: int, message_id: Optional[int]) -> None:
    _log_menu_state(admin_uid, "user_detail:enter")
    # safety: перед рендером деталей убедимся, что меню зафиксировано в state
    try:
        post_menu = get_menu_message_id(int(admin_uid))
        if not post_menu:
            kb = await get_main_menu(int(admin_uid))
            if kb is not None:
                try:
                    mid = await send_root_menu_singleton(int(admin_uid), kb)
                    logger.warning("[ratings_admin] (detail) menu restored via send_root_menu_singleton uid=%s mid=%s", int(admin_uid), mid)
                    mm = getattr(state, "menu_message_id", None)
                    if isinstance(mm, dict):
                        mm[int(admin_uid)] = int(mid)
                    elif isinstance(mm, int):
                        setattr(state, "menu_message_id", int(mid))
                    else:
                        setattr(state, "menu_message_id", {int(admin_uid): int(mid)})
                except Exception:
                    pass
        else:
            try:
                mm = getattr(state, "menu_message_id", None)
                if isinstance(mm, dict):
                    mm[int(admin_uid)] = int(post_menu)
                elif isinstance(mm, int):
                    setattr(state, "menu_message_id", int(post_menu))
                else:
                    setattr(state, "menu_message_id", {int(admin_uid): int(post_menu)})
            except Exception:
                pass
    except Exception:
        pass
    _log_menu_state(admin_uid, "user_detail:pre-render")
    stats = await get_user_stats(target_uid)
    name = await short_name(target_uid)
    score = int(stats.get("score", 0))
    baseline = int(stats.get("baseline", 0))
    manual = int(stats.get("manual_delta", 0))
    groups = stats.get("groups", {})
    events = stats.get("events", [])
    # groups already include manual_delta (services.get_user_stats ensures this)
    groups_display = dict(groups or {})

    weeks_map = await get_cant_work_weeks(target_uid)
    weeks_list = weeks_map.get(target_uid, []) if isinstance(weeks_map, dict) else []

    text = (
        f"⭐ {name} — <b>{score}</b> / 100\n\n"
        f"База (Светофор): {baseline}\n"
        f"Цепочка «не могу»: {_cant_chain_label(weeks_list)}\n\n"
    f"Разбор событий:\n{_format_groups(groups_display)}\n\n"
        "Последние события:\n"
        f"{_format_events(events)}"
    )

    # Prefer editing report message if present (to keep report in place when opening details)
    msg_id_to_use = message_id
    try:
        cur_leader = getattr(state, "current_poll_leader", None)
        report_mid = getattr(state, "personal_report_message_id", None)
        if (not msg_id_to_use) and isinstance(cur_leader, int) and int(cur_leader) == int(admin_uid) and isinstance(report_mid, int) and report_mid > 0:
            msg_id_to_use = int(report_mid)
    except Exception:
        pass
    _log_menu_state(admin_uid, "before-dm_singleton:user_detail")
    await dm_singleton_edit_or_send(
        admin_uid,
        msg_id_to_use,
        text,
        parse_mode="HTML",
        reply_markup=_user_keyboard(target_uid),
    )
    _log_menu_state(admin_uid, "after-dm_singleton:user_detail")


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


@router.callback_query(F.data.startswith("ratings_admin_apply_"))
async def ratings_admin_apply(callback: types.CallbackQuery) -> None:
    """Руководитель применяет преднастроенное событие (причину).
    callback.data: ratings_admin_apply_{target_uid}_{code}
    """
    if not _is_leader(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        _, _, tail = (callback.data or "").partition("ratings_admin_apply_")
        target_s, code = tail.split("_", 1)
        target_uid = int(target_s)
    except Exception:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    # найти delta по коду
    delta = None
    for c, _, d in _LEADER_REASONS:
        if c == code:
            delta = int(d)
            break
    if delta is None:
        await callback.answer("Неизвестная причина", show_alert=True)
        return
    try:
        from services.ratings import apply_leader_event
        reason = f"leader:{int(callback.from_user.id)}:{code}"
        logger.info("[ratings_admin_apply] applying leader event target=%s code=%s delta=%s by=%s", target_uid, code, delta, int(callback.from_user.id))
        await apply_leader_event(target_uid, delta, kind=code, meta={"by": int(callback.from_user.id), "reason": reason})
        await callback.answer("Применено")
    except Exception as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)
        return
    await _render_user_detail(callback.from_user.id, target_uid, getattr(callback.message, "message_id", None))


@router.message(Command("rating_debug"))
async def rating_debug_cmd(message: types.Message) -> None:
    """Usage: /rating_debug <uid> — limited debugging for leader only"""
    if not _is_leader(message.from_user.id):
        await message.answer("Недоступно.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /rating_debug <uid>")
        return
    target_uid = int(parts[1])
    try:
        # gather raw pieces
        stats = await get_user_stats(target_uid)
        # events_total and groups available in stats
        base = int(stats.get("baseline", 0))
        events_total = int(stats.get("events_total", 0))
        manual = int(stats.get("manual_delta", 0))
        raw_total = base + events_total + manual
        try:
            cap = int((getattr(settings, "RATING", {}) or {}).get("CAP_MAX", 100))
        except Exception:
            cap = 100
        events = stats.get("events", [])[:20]
        lines = [
            f"uid={target_uid}",
            f"base={base}",
            f"events_total={events_total}",
            f"manual_delta={manual}",
            f"raw_total={raw_total}",
            f"cap={cap}",
            "Последние события:",
        ]
        for e in events:
            lines.append(f" - {e.get('when_ts')} {e.get('kind')} {int(e.get('points') or 0)} meta={e.get('meta')}")
        await message.answer("\n".join(lines))
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")


# 2025-09-17 · модуль рейтинга: выровнено под SSOT.
