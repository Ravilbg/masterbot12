# handlers/polls_distribution.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Ручное управление распределением (этап лидера).
После «Утвердить» распределение фиксируется и запускается цикл подтверждений.

Версия v14.9-cycle • 2025-08-12
──────────────────────────────────────────────────────────────────────────────
• Единый коллбэк «Утвердить»: poll_approve_{deal_id}.
• Источник правды по составу — state.distribution_cache / poll_details.distribution.
• Автораспределение main/assist из ответов опроса + Светофор; офлайн-фолбэк.
• Поддержка legacy-ключей main_leaders/assistants.
• Уведомление уходит в POLLS_CHAT_ID / LEADERS_CHAT_ID / ADMIN_CHAT_ID.
• В уведомлении рабочая кнопка «🎲 Личный кабинет» (deep-link /start=my_games).
• Идемпотентность «Утвердить»: повторный клик не дублирует фиксацию/уведомления.
• Перерисовка «Мои игры» коалесцируется (один редрав на батч uid).
"""

from __future__ import annotations

# ════════════════════════════════════════════════════════════════════
# [0] IMPORTS
# ════════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Set, Tuple, Optional

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from core.config import settings
from core.state import state
from handlers.my_games import redraw_my_games
import handlers.polls_lifecycle as plc  # локальный импорт, чтобы избежать циклов
from services.gsheets import get_user_status_from_svetofor

logger = logging.getLogger(__name__)
router = Router()

# алиас на проверку готовности сделки (из lifecycle)
_is_deal_ready = plc._is_deal_ready

async def _try_sync_report() -> None:
    """
    Совместимый вызов перерисовки отчёта после «Утвердить».
    Если в текущей версии lifecycle нет sync_report — тихо пропускаем.
    """
    try:
        fn = getattr(plc, "sync_report", None)
        if callable(fn):
            await fn()
    except Exception as e:
        logger.warning("[polls_dist] sync_report skipped: %s", e)

# История изменений: [0] обновлён 2025-08-13 — добавлен _try_sync_report(), Optional в типах


# ════════════════════════════════════════════════════════════════════
# [1] УТИЛИТЫ: нормализация, commit в state, формат уведомлений
# ════════════════════════════════════════════════════════════════════
"""
Назначение:
• нормализовать состав (UID) из distribution_cache / poll_details;
• инварианта «1 uid → 1 роль» (main > assist > admin);
• собрать «слоты» под «Мои игры»: lead1/assistant1/admin/trainee «Имя Ф.<суффикс>|uid»;
• записать утверждённый состав в locked_distribution + poll_details.distribution + distribution_cache;
• аккуратно собрать заголовок «Название. Дата Время. Пакет. N чел.»;
• форматировать список участников «• Имя Ф.1 / • Имя Ф.2 / • Имя Ф. Адм / • Имя Ф. Стаж»;
• локально перекрасить кнопку «Утвердить» → «✅ Утверждено».
"""

def _ensure_state_structs() -> None:
    if not hasattr(state, "assigned_index") or state.assigned_index is None:
        state.assigned_index = {}            # dict[int, set[int]]
    if not hasattr(state, "locked_distribution") or state.locked_distribution is None:
        state.locked_distribution = {}       # dict[int, dict[str,str]]
    if not hasattr(state, "pending_confirmations") or state.pending_confirmations is None:
        state.pending_confirmations = {}     # dict[int, dict]
    if not hasattr(state, "distribution_cache") or state.distribution_cache is None:
        state.distribution_cache = {}        # dict[str, dict[str,str]]
    if not hasattr(state, "poll_details") or state.poll_details is None:
        state.poll_details = {}              # dict[int, dict]
    if not hasattr(state, "poll_distribution") or state.poll_distribution is None:
        state.poll_distribution = {}         # dict[int, dict]

def _parse_uid(val: Any) -> Optional[int]:
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        try:
            return int(s)
        except ValueError:
            return None
    return None

def _as_user_list(v: Any) -> List[int]:
    out: List[int] = []
    if v is None:
        return out
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        u = _parse_uid(v)
        return [u] if u is not None else out
    if isinstance(v, (list, tuple, set)):
        for x in v:
            out.extend(_as_user_list(x))
    return out

def _normalize_roles(raw: Dict[str, Any]) -> Dict[str, List[int]]:
    main_val = raw.get("main", raw.get("main_leaders"))
    assist_val = raw.get("assist", raw.get("assistants"))
    admin_val = raw.get("admin")
    return {"main": _as_user_list(main_val), "assist": _as_user_list(assist_val), "admin": _as_user_list(admin_val)}

def _dedupe_roles(roles: Dict[str, List[int]]) -> Dict[str, List[int]]:
    seen: Set[int] = set()
    out: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}
    for u in roles.get("main", []):
        if u and u not in seen:
            out["main"].append(u); seen.add(u)
    for u in roles.get("assist", []):
        if u and u not in seen:
            out["assist"].append(u); seen.add(u)
    for u in roles.get("admin", []):
        if u and u not in seen:
            out["admin"].append(u); seen.add(u)
    return out

def _uids_from_roles(roles: Dict[str, List[int]]) -> Set[int]:
    return set(roles.get("main", [])) | set(roles.get("assist", [])) | set(roles.get("admin", []))

def _extract_distribution_from_cache(deal_id: int) -> Optional[Dict[str, List[int]]]:
    dc = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id))
    if isinstance(dc, dict):
        return _normalize_roles(dc)
    details = (getattr(state, "poll_details", {}) or {}).get(deal_id) or {}
    if isinstance(details.get("distribution"), dict):
        return _normalize_roles(details["distribution"])
    dist3 = (getattr(state, "poll_distribution", {}) or {}).get(deal_id)
    if isinstance(dist3, dict):
        return _normalize_roles(dist3)
    return None

async def _derive_team_roles(deal_id: int) -> Dict[str, List[int]]:
    """Компонуем из ответов + Светофора, если кэши пусты."""
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id") or 0) == deal_id), None)
    if not deal:
        return {"main": [], "assist": [], "admin": []}

    game_name = deal.get("game_name") or deal.get("name") or ""
    cfg = plc._role_cfg(game_name)  # type: ignore[attr-defined]
    need_main, need_assist = int(cfg["main_leaders"]), int(cfg["assistants"])

    team: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}
    used: Set[int] = set()

    for pdata in (state.responses or {}).values():
        users = (pdata.get("deals") or {}).get(deal_id, [])
        if not users:
            continue
        for u in users:
            uid = int(u.get("user_id") or 0)
            if not uid or uid in used:
                continue
            status = get_user_status_from_svetofor(uid, game_name)
            if asyncio.iscoroutine(status):
                status = await status
            if status == "green":
                if len(team["main"]) < need_main:
                    team["main"].append(uid); used.add(uid); continue
                if len(team["assist"]) < need_assist:
                    team["assist"].append(uid); used.add(uid); continue
            elif status == "yellow":
                if len(team["assist"]) < need_assist:
                    team["assist"].append(uid); used.add(uid); continue

    # админ — либо из кэша, либо по «админ доступен»
    cached = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id)) or {}
    if cached.get("admin"):
        team["admin"] = _as_user_list(cached.get("admin"))
    if not team["admin"]:
        assigned = set(team["main"]) | set(team["assist"])
        for pdata in (state.responses or {}).values():
            for adm in (pdata.get("admin_available") or []):
                uid = int(adm.get("user_id") or 0)
                if uid and uid not in assigned:
                    team["admin"] = [uid]
                    break
            if team["admin"]:
                break

    return team

async def _get_current_team(deal_id: int, invoker_uid: Optional[int] = None) -> Dict[str, List[int]]:
    dist = _extract_distribution_from_cache(deal_id)
    if dist is None or (not dist.get("main") and not dist.get("assist")):
        derived = await _derive_team_roles(deal_id)
        if dist and dist.get("admin") and not derived.get("admin"):
            derived["admin"] = list(dist["admin"])
        return _dedupe_roles(derived)
    return _dedupe_roles(dist)

async def _short_name(uid: int) -> str:
    try:
        from core.db import get_user_info
        ui = get_user_info(uid)
        if asyncio.iscoroutine(ui):
            ui = await ui
        ui = ui or {}
    except Exception:
        ui = {}
    fn = (ui.get("first_name") or "").strip()
    last = (ui.get("last_name") or "").strip()
    ln_i = (last[:1].upper() + ".") if last else ""
    return (f"{fn} {ln_i}".strip() or str(uid)).strip()

async def _fmt(uid_: int, role_key: str) -> str:
    name = await _short_name(uid_)
    suffix = {"main": ".1", "assist": ".2", "admin": ".Адм", "trainee": ".Стаж"}.get(role_key, "")
    return f"{name}{suffix}|{uid_}".strip()

async def _to_slot_distribution(deal_id: int, roles: Dict[str, List[int]]) -> Dict[str, str]:
    slot: Dict[str, str] = {}
    idx = 1
    for uid in roles.get("main", []):
        slot[f"lead{idx}"] = await _fmt(uid, "main"); idx += 1
    idx = 1
    for uid in roles.get("assist", []):
        slot[f"assistant{idx}"] = await _fmt(uid, "assist"); idx += 1
    if roles.get("admin"):
        slot["admin"] = await _fmt(roles["admin"][0], "admin")

    # trainee — из legacy-кэша, если был указан
    raw = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
    if isinstance(raw, dict) and raw.get("trainee"):
        slot["trainee"] = str(raw["trainee"])
    return slot

def _deal_title(deal_id: int) -> str:
    try:
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == deal_id:
                return str(d.get("game_name") or d.get("name") or f"Сделка #{deal_id}")
        title = (getattr(state, "deal_titles", {}) or {}).get(deal_id)
        return str(title) if title else f"Сделка #{deal_id}"
    except Exception:
        return f"Сделка #{deal_id}"

def _deal_header_sentence(deal_id: int) -> str:
    d = next((x for x in (state.current_poll_deals or []) if int(x.get("id") or 0) == deal_id), None)
    meta = (getattr(state, "deals_index", {}) or {}).get(deal_id, {})
    title = str((d or {}).get("game_name") or (d or {}).get("name") or meta.get("title") or f"Сделка #{deal_id}").strip()
    if d and d.get("event_datetime"):
        try:
            date_s = d["event_datetime"].strftime("%d.%m.%Y")
        except Exception:
            date_s = str(d.get("event_date") or meta.get("date") or "")
    else:
        date_s = str((d or {}).get("event_date") or meta.get("date") or "")
    time_s = str((d or {}).get("event_time") or meta.get("time") or "")
    pkg = str((d or {}).get("package") or meta.get("package") or "").strip()
    players = str((d or {}).get("players") or (d or {}).get("players_count") or meta.get("players") or "")
    parts = [title, f"{date_s} {time_s}".strip(), pkg, (f"{players} чел." if players else "")]
    return (". ".join([p for p in parts if p]).strip().rstrip(".") + ".")

async def _team_bulleted_lines(roles: Dict[str, List[int]], deal_id: int) -> List[str]:
    lines: List[str] = []
    for uid in roles.get("main", []):
        lines.append(f"• {await _short_name(uid)}.1".replace("..1", ".1"))
    for uid in roles.get("assist", []):
        lines.append(f"• {await _short_name(uid)}.2".replace("..2", ".2"))
    for uid in roles.get("admin", []):
        lines.append(f"• {await _short_name(uid)} Адм")
    raw = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
    if isinstance(raw, dict) and raw.get("trainee"):
        try:
            t_uid = _parse_uid(raw.get("trainee"))
            if t_uid:
                lines.append(f"• {await _short_name(t_uid)} Стаж")
        except Exception:
            pass
    return lines or ["• —"]

async def _approval_announce_kb() -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton
    from aiogram import Bot
    bot = Bot.get_current()
    me = await bot.get_me()
    url = f"https://t.me/{(me.username or 'bot')}?start=my_games"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Личный кабинет", url=url)]])

def _mark_approved_on_message_kb(callback: CallbackQuery, deal_id: int) -> Optional[InlineKeyboardMarkup]:
    try:
        msg = getattr(callback, "message", None)
        if not msg or not hasattr(msg, "reply_markup"):
            return None
        kb = msg.reply_markup
        if not isinstance(kb, InlineKeyboardMarkup):
            return None
        from aiogram.types import InlineKeyboardButton
        new_rows: List[List[InlineKeyboardButton]] = []
        for row in (kb.inline_keyboard or []):
            new_row: List[InlineKeyboardButton] = []
            for btn in row:
                if getattr(btn, "callback_data", "") == f"poll_approve_{deal_id}":
                    new_row.append(InlineKeyboardButton(text="✅ Утверждено", callback_data="noop"))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_rows)
    except Exception:
        logger.exception("[approve] failed to rebuild keyboard")
        return None

async def _resolve_notify_chat_id(bot) -> Optional[int]:
    """
    Возвращает первый доступный чат для уведомлений:
    POLLS_CHAT_ID → LEADERS_CHAT_ID → state.admin_chat_id → ADMIN_CHAT_ID.
    Валидирует доступ через get_chat, чтобы не ловить 'chat not found'.
    """
    candidates = [
        getattr(settings, "POLLS_CHAT_ID", None),
        getattr(settings, "LEADERS_CHAT_ID", None),
        getattr(state, "admin_chat_id", None),
        getattr(settings, "ADMIN_CHAT_ID", None),
    ]
    for cid in candidates:
        if not cid:
            continue
        try:
            cid_int = int(str(cid).strip())
            await bot.get_chat(cid_int)
            return cid_int
        except Exception:
            logger.warning("[polls_dist] notify chat %s not accessible", cid)
    return None

async def _commit_locked_distribution_to_state(deal_id: int, roles: Dict[str, List[int]]) -> Dict[str, str]:
    """
    Синхронизируем точки правды (ФОРМАТ совместим с «Мои игры»):
    • locked_distribution[deal_id] ← {'lead1': 'Имя Ф.1|uid', 'assistant1': 'Имя Ф.2|uid', 'admin': 'Имя Ф.Адм|uid', 'trainee': 'Имя Ф.Стаж|uid'?}
    • pending_confirmations[deal_id]['distribution'] ← тот же dict
    • distribution_cache[str(deal_id)] ← тот же dict
    • poll_details[deal_id]['distribution'] ← тот же dict
    • assigned_index[uid] ← deal_id
    """
    _ensure_state_structs()
    slots = await _to_slot_distribution(deal_id, roles)

    state.locked_distribution[deal_id] = dict(slots)
    state.pending_confirmations[deal_id] = {"distribution": dict(slots), "confirmed": set()}

    state.distribution_cache[str(deal_id)] = dict(slots)
    pd = state.poll_details.setdefault(deal_id, {})
    pd["distribution"] = dict(slots)

    all_uids: Set[int] = set()
    for v in slots.values():
        u = _parse_uid(v)
        if u:
            all_uids.add(u)
    for uid in all_uids:
        idx = state.assigned_index.setdefault(uid, set())
        idx.add(deal_id)

    logger.debug("[polls_dist] deal %d locked+committed; slots=%s", deal_id, slots)
    return slots

# История изменений: [1] обновлён 2025-08-13 — добавлен _resolve_notify_chat_id с валидацией get_chat


# ════════════════════════════════════════════════════════════════════
# [1] INLINE-КЛАВИАТУРА (резерв под действия)
# ════════════════════════════════════════════════════════════════════
def distribution_actions_markup() -> InlineKeyboardMarkup:
    """Нижняя action-панель для отчёта лидеру (зарезервировано под будущее)."""
    return InlineKeyboardMarkup(inline_keyboard=[])

# История изменений: [1-inline] добавлен 2025-08-13, без логики (резерв)

# ════════════════════════════════════════════════════════════════════
# [1.1] Коалесцирование перерисовок «Мои игры» (фикс гонок «пылесоса»)
# ════════════════════════════════════════════════════════════════════
def _queue_redraw_my_games(uids: Set[int], delay_sec: float = 0.15) -> None:
    """
    Складываем uid в аккумулятор и планируем ОДНУ задачу,
    которая через короткую паузу перерисует дашборд для каждого uid ровно один раз.
    """
    if not uids:
        return

    acc: Set[int] = getattr(state, "_redraw_accum", set())
    acc |= set(uids)
    setattr(state, "_redraw_accum", acc)

    task: asyncio.Task | None = getattr(state, "_redraw_task", None)
    if task and not task.done():
        return

    async def _runner():
        try:
            await asyncio.sleep(delay_sec)
            batch: Set[int] = getattr(state, "_redraw_accum", set())
            setattr(state, "_redraw_accum", set())
            for uid in sorted(batch):
                try:
                    await redraw_my_games(uid)
                except Exception as e:
                    logger.error("[redraw_coalesce] uid=%s failed: %s", uid, e)
        finally:
            setattr(state, "_redraw_task", None)

    setattr(state, "_redraw_task", asyncio.create_task(_runner()))

# История изменений: [1.1] добавлен 2025-08-13, коалесцирование редравов


# ════════════════════════════════════════════════════════════════════
# [2] INLINE-КЛАВИАТУРА (резерв под действия)
# ════════════════════════════════════════════════════════════════════
"""
Здесь раньше повторно определялась distribution_actions_markup(), из-за чего
происходило перекрытие функции и появлялись предупреждения линтера/IDE.

Исправление:
— Дубликат удалён. Используем единственную реализацию из секции [1].
— Блок оставлен как «резерв» без кода, чтобы сохранить нумерацию и стиль.
"""

# намеренно пусто — актуальная функция distribution_actions_markup() объявлена в [1]

# История изменений: 2025-08-12 — удалён дублирующийся def distribution_actions_markup()


# ════════════════════════════════════════════════════════════════════
# [3] HANDLER: Утвердить одну игру (без автопереходов)
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    """
    Утверждение одной игры. Без автопереходов:
    • только перекраска кнопки в текущем сообщении,
    • запись состава в кэши/индексы в формате «слотов»,
    • чат-уведомление с deep-link «🎲 Личный кабинет».
    """
    try:
        deal_id = int((callback.data or "").rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Ошибка: неизвестный формат callback.", show_alert=True)
        return

    # уже утверждено?
    if deal_id in (getattr(state, "locked_distribution", {}) or {}):
        try:
            kb = _mark_approved_on_message_kb(callback, deal_id)
            if kb and callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await callback.answer("Уже утверждено ✅")
        return

    if not await _is_deal_ready(deal_id):
        await callback.answer("Минимальный состав ещё не набран.", show_alert=True)
        return

    roles = await _get_current_team(deal_id, callback.from_user.id)
    if not _uids_from_roles(roles):
        logger.warning("[approve] deal %d has empty roles after normalization", deal_id)
        await callback.answer("Нет текущего распределения. Откройте детали и расставьте роли.", show_alert=True)
        return

    # фиксируем и синхронизируем кэши (включая poll_details.distribution)
    await _commit_locked_distribution_to_state(deal_id, roles)

    # перекраска кнопки в текущем сообщении
    try:
        kb = _mark_approved_on_message_kb(callback, deal_id)
        if kb and callback.message:
            await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception as e:
        logger.debug("[approve] edit_reply_markup failed: %s", e)

    try:
        await callback.answer("Игра утверждена ✅")
    except Exception:
        pass

    # чат-уведомление
    try:
        bot = callback.message.bot if callback.message else None
        if bot:
            chat_id = await _resolve_notify_chat_id(bot)
            if chat_id is not None:
                head = _deal_header_sentence(deal_id)
                lines = await _team_bulleted_lines(roles, deal_id)
                text = (
                    f"✅ Состав команды на игру {head} утверждён.\n"
                    + "\n".join(lines)
                    + "\nПодтвердите своё участие в личном кабинете: «🎲 Мои игры» → «✅ Подтвердить»."
                )
                kb2 = await _approval_announce_kb()
                await bot.send_message(chat_id, text, reply_markup=kb2)
            else:
                logger.error("[approve] no available chat for notify; skipped")
    except Exception as e:
        logger.error("[approve] notify chat failed: %s", e)

    # мягкая перерисовка отчёта лидеру
    await _try_sync_report()

    logger.info("[approve] deal %d approved by %d; roles=%s", deal_id, callback.from_user.id, roles)

# История изменений: [3] обновлён 2025-08-13 — notify через _resolve_notify_chat_id, _try_sync_report



# ════════════════════════════════════════════════════════════════════
# [4] HANDLER: Утвердить все готовые (батч, без автопереходов)
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data == "approve_all_ready")
async def poll_approve_all_ready_handler(callback: CallbackQuery) -> None:
    """
    Утверждает все игры, где набран минимальный состав.
    Без автопереходов/редравов «Моих игр». На каждую игру — корректное чат-уведомление.
    """
    try:
        await callback.answer("Обрабатываю…")
    except Exception:
        pass

    approved: List[int] = []
    skipped: List[Tuple[int, str]] = []

    bot = callback.message.bot if callback.message else None
    chat_id: Optional[int] = None
    if bot:
        chat_id = await _resolve_notify_chat_id(bot)

    for deal in list(state.current_poll_deals or []):
        did = int(deal.get("id") or 0)
        if not did:
            continue
        try:
            if not await _is_deal_ready(did):
                skipped.append((did, "not_ready"))
                continue
            if did in (getattr(state, "locked_distribution", {}) or {}):
                approved.append(did)
                continue

            roles = await _get_current_team(did, callback.from_user.id)
            if not _uids_from_roles(roles):
                skipped.append((did, "no_roles"))
                continue

            await _commit_locked_distribution_to_state(did, roles)
            approved.append(did)

            # чат-уведомление «как в одиночном approve»
            if bot and chat_id is not None:
                try:
                    head = _deal_header_sentence(did)
                    lines = await _team_bulleted_lines(roles, did)
                    text = (
                        f"✅ Состав команды на игру {head} утверждён.\n"
                        + "\n".join(lines)
                        + "\nПодтвердите своё участие в личном кабинете: «🎲 Мои игры» → «✅ Подтвердить»."
                    )
                    kb2 = await _approval_announce_kb()
                    await bot.send_message(chat_id, text, reply_markup=kb2)
                except Exception as e:
                    logger.error("[approve_all] notify chat failed for %s: %s", did, e)

        except Exception as e:
            logger.warning("[approve_all] deal %s failed: %s", did, e)
            skipped.append((did, "exception"))

    try:
        if approved:
            await callback.answer(f"Утверждено игр: {len(approved)} ✅")
        else:
            await callback.answer("Нет готовых игр для утверждения.", show_alert=True)
    except Exception:
        pass

    await _try_sync_report()

# История изменений: [4] обновлён 2025-08-13 — единый chat resolver, _try_sync_report


# ════════════════════════════════════════════════════════════════════
# [5] ПРОЧИЕ HANDLERS: stop / back
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith("poll_stop_"))
async def poll_stop_game_handler(callback: CallbackQuery) -> None:
    try:
        deal_id = int((callback.data or "").rsplit("_", 1)[-1])
    except Exception:
        try:
            await callback.answer()
        except Exception:
            pass
        return
    if not hasattr(state, "deal_force_closed") or state.deal_force_closed is None:
        state.deal_force_closed = set()
    state.deal_force_closed.add(deal_id)
    try:
        await callback.answer("Набор остановлен.")
    except Exception:
        pass
    logger.info("[details] deal %d force-stopped by %d", deal_id, callback.from_user.id)

@router.callback_query(lambda c: c.data and c.data.startswith("poll_back_"))
async def poll_back_handler(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass
    await _try_sync_report()

# История изменений: [5] обновлён 2025-08-13 — back вызывает _try_sync_report()

# ════════════════════════════════════════════════════════════════════
# [99] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    """Быстрые проверки нормализации и форматирования (без внешних сервисов)."""
    raw = {"main": [1, "Иван И.|101"], "assist": ["202"], "admin": ["bad", 303, "303", "Петр|404", ["505", ["Сергей|606"]]]}
    norm = _normalize_roles(raw)
    assert norm == {"main": [1, 101], "assist": [202], "admin": [303, 303, 404, 505, 606]}
    dd = _dedupe_roles(norm)
    assert dd == {"main": [1, 101], "assist": [202], "admin": [303, 404, 505, 606]}
    assert _uids_from_roles(dd) == {1, 101, 202, 303, 404, 505, 606}
    print("handlers.polls_distribution ✅ tests passed")

if __name__ == "__main__":
    import asyncio as _a, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    _a.run(_test())

# История изменений: [99] добавлен 2025-08-13, smoke-тест нормализации
