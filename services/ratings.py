from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from core.config import settings
from core.db import init_db
from services.gsheets import get_user_traffic_light

DB_PATH = Path(settings.CHECKLISTS_DB_PATH)

_RATING_CFG: Dict[str, Any] = settings.RATING or {}
_WEIGHTS: Dict[str, float] = {
    str(key): float(value)
    for key, value in ((_RATING_CFG.get("WEIGHTS", {}) or {}).items())
}
_WINDOW_SECONDS = max(1, int(_RATING_CFG.get("WINDOW_DAYS", 30)) * 86400)
_CAP_MIN = int(_RATING_CFG.get("CAP_MIN", 0))
_CAP_MAX = int(_RATING_CFG.get("CAP_MAX", 100))
_YELLOW_WEIGHT = float(_RATING_CFG.get("YELLOW_WEIGHT", 0.0))

_MSK_TZ = timezone(timedelta(hours=3))

_EVENT_GROUPS: Dict[str, str] = {
    "poll_reply_d12": "poll_reply",
    "poll_reply_d36": "poll_reply",
    "poll_reply_late": "poll_reply",
    "confirm_d24": "confirm",
    "confirm_d36": "confirm",
    "confirm_late": "confirm",
    "no_reply_penalty_w1": "penalties",
    "no_reply_penalty_w2": "penalties",
    "cant_work_2w_row": "penalties",
    "cant_work_3w_row": "penalties",
    "game_main": "games",
    "game_assist": "games",
    "game_admin": "games",
    "game_trainee": "games",
    "learn_game": "learning",
    "urgent_replacement": "urgent",
    "manual_adjust": "manual",
    # leader events
    "bonus_hero": "manual",
    "miss_meeting": "manual",
    "late": "manual",
    "poor_review": "manual",
    "attend_meeting": "manual",
}

__all__ = [
    "record_event",
    "get_user_score",
    "get_scores",
    "adjust_score",
    "get_user_stats",
    "recompute_baseline_for_all",
    "has_flag",
    "set_flag",
    "get_flag",
    "get_cant_work_weeks",
    "apply_leader_event",
]


def _now_ts() -> int:
    return int(time.time())


def _clamp_score(value: float) -> int:
    return max(_CAP_MIN, min(_CAP_MAX, int(round(value))))


def _bucket_reply(delta_hours: float) -> str:
    if delta_hours <= 12:
        return "d12"
    if delta_hours <= 36:
        return "d36"
    return "late"


def _bucket_confirm(delta_hours: float) -> str:
    if delta_hours <= 24:
        return "d24"
    if delta_hours <= 36:
        return "d36"
    return "late"


def _weight(kind: str) -> float:
    return float(_WEIGHTS.get(kind, 0.0))


def _decode_meta(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    return {}


def _connect() -> aiosqlite.Connection:
    return aiosqlite.connect(DB_PATH)


async def _remember_cant_work(uid: int, when_ts: int) -> None:
    dt = datetime.fromtimestamp(when_ts, tz=_MSK_TZ)
    iso = dt.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO leader_cantwork_weeks (uid, week_iso) VALUES (?, ?)",
            (int(uid), week_key),
        )
        await db.commit()


async def record_event(
    uid: int,
    kind: str,
    meta: Optional[Dict[str, Any]] = None,
    when: Optional[int] = None,
    poll_id: Optional[str] = None,
    deal_id: Optional[str] = None,
) -> None:
    await init_db()
    when_ts = int(when if when is not None else _now_ts())
    payload: Dict[str, Any] = dict(meta or {})
    final_kind = kind

    if kind == "poll_reply":
        opened = int(payload.get("t_open", when_ts) or when_ts)
        delta = max(0.0, (when_ts - opened) / 3600.0)
        bucket = _bucket_reply(delta)
        payload["delta_hours"] = delta
        payload["bucket"] = bucket
        final_kind = f"poll_reply_{bucket}"
    elif kind == "confirm":
        assigned = int(payload.get("t_assign", when_ts) or when_ts)
        delta = max(0.0, (when_ts - assigned) / 3600.0)
        bucket = _bucket_confirm(delta)
        payload["delta_hours"] = delta
        payload["bucket"] = bucket
        final_kind = f"confirm_{bucket}"
    elif kind == "cant_work":
        await _remember_cant_work(uid, when_ts)
        return
    else:
        final_kind = kind

    payload["recorded_kind"] = final_kind
    points = int(round(_weight(final_kind)))

    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO leader_rating_events (uid, kind, points, when_ts, meta, poll_id, deal_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(uid),
                final_kind,
                points,
                when_ts,
                json.dumps(payload, ensure_ascii=False) if payload else None,
                poll_id,
                deal_id,
            ),
        )
        await db.commit()




async def get_flag(uid: int, poll_id: str, flag: str) -> Optional[str]:
    await init_db()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT value FROM leader_rating_flags WHERE uid = ? AND poll_id = ? AND flag = ?",
            (int(uid), poll_id or "", flag),
        )
        row = await cur.fetchone()
    return str(row["value"]) if row and row["value"] is not None else None

async def set_flag(uid: int, poll_id: str, flag: str, value: str = "1") -> None:
    await init_db()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR REPLACE INTO leader_rating_flags (uid, poll_id, flag, value) VALUES (?, ?, ?, ?)",
            (int(uid), poll_id or "", flag, value),
        )
        await db.commit()


async def has_flag(uid: int, poll_id: str, flag: str) -> bool:
    await init_db()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT value FROM leader_rating_flags WHERE uid = ? AND poll_id = ? AND flag = ?",
            (int(uid), poll_id or "", flag),
        )
        row = await cur.fetchone()
    return row is not None


async def _manual_delta(uid: int) -> int:
    await init_db()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT manual_delta FROM leader_rating_adjust WHERE uid = ?", (int(uid),))
        row = await cur.fetchone()
    return int(row["manual_delta"]) if row else 0


async def adjust_score(uid: int, delta: int, reason: str) -> None:
    await init_db()
    now_ts = _now_ts()
    current = await _manual_delta(uid)
    # ensure we don't push the raw score beyond CAP_MAX
    # compute events_total (excluding manual_adjust)
    rows = await _events_in_window(uid, _WINDOW_SECONDS)
    events_total = 0
    for row in rows:
        kind = str(row["kind"] or "")
        if kind == "manual_adjust":
            continue
        events_total += int(row["points"] or 0)
    base = await _baseline(uid)
    # remaining room to CAP_MAX
    remaining = int(_CAP_MAX - (base + events_total + current))
    # allow negative deltas freely; clip positive deltas to remaining
    delta_int = int(delta)
    if delta_int > 0:
        if remaining <= 0:
            # nothing to add
            delta_int = 0
        else:
            delta_int = min(delta_int, remaining)
    new_delta = current + int(delta_int)
    payload = {"delta": int(delta), "reason": reason}
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO leader_rating_adjust(uid, manual_delta, updated_at) VALUES(?, ?, ?)"
            " ON CONFLICT(uid) DO UPDATE SET manual_delta = excluded.manual_delta, updated_at = excluded.updated_at",
            (int(uid), new_delta, now_ts),
        )
        await db.execute(
            "INSERT INTO leader_rating_events (uid, kind, points, when_ts, meta, poll_id, deal_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(uid), "manual_adjust", 0, now_ts, json.dumps(payload, ensure_ascii=False), None, None),
        )
        await db.commit()


async def apply_leader_event(uid: int, delta: int, kind: str, meta: Optional[Dict[str, Any]] = None) -> None:
    """Apply an event initiated by leader/manager. This creates a dedicated event (kind) with points=delta.
    It does NOT touch manual_delta (leader events are normal events and visible in the timeline under their own kind).
    """
    await init_db()
    when_ts = _now_ts()
    payload = dict(meta or {})
    payload["recorded_by_leader"] = True
    # Prevent positive leader events from pushing raw score beyond CAP_MAX
    try:
        # sum current events (excluding manual_adjust)
        rows = await _events_in_window(uid, _WINDOW_SECONDS)
        events_total = 0
        for row in rows:
            k = str(row["kind"] or "")
            if k == "manual_adjust":
                continue
            events_total += int(row["points"] or 0)
        base = await _baseline(uid)
        manual = await _manual_delta(uid)
        remaining = int(_CAP_MAX - (base + events_total + int(manual)))
        delta_int = int(delta)
        if delta_int > 0:
            if remaining <= 0:
                # nothing to add; skip creating zero-point leader event
                try:
                    import logging
                    logging.getLogger(__name__).info("[apply_leader_event] skip: uid=%s kind=%s delta=%s remaining=0", uid, kind, delta_int)
                except Exception:
                    pass
                return
            if delta_int > remaining:
                delta_int = remaining
        else:
            delta_int = int(delta_int)
    except Exception:
        delta_int = int(delta)
    # debug log for tracing leader-applied events
    try:
        import logging
        logging.getLogger(__name__).info("[apply_leader_event] uid=%s kind=%s delta=%s meta=%s", int(uid), str(kind), int(delta), payload)
    except Exception:
        pass
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO leader_rating_events (uid, kind, points, when_ts, meta, poll_id, deal_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(uid), str(kind), int(delta_int), when_ts, json.dumps(payload, ensure_ascii=False) if payload else None, None, None),
        )
        await db.commit()


async def _events_in_window(uid: int, window_seconds: int) -> List[aiosqlite.Row]:
    cutoff = _now_ts() - window_seconds
    await init_db()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT uid, kind, points, when_ts, meta, poll_id, deal_id FROM leader_rating_events"
            " WHERE uid = ? AND when_ts >= ? ORDER BY when_ts DESC",
            (int(uid), cutoff),
        )
        rows = await cur.fetchall()
    return rows


async def _baseline(uid: int) -> int:
    snapshot: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        snapshot = await get_user_traffic_light(int(uid))
    green = int(snapshot.get("green") or 0)
    yellow = int(snapshot.get("yellow") or 0)
    total = int(snapshot.get("total") or 0)
    if total <= 0:
        total = green + yellow + int(snapshot.get("red") or 0)
    if total <= 0:
        base = 50
    else:
        ratio = (green + yellow * _YELLOW_WEIGHT) / float(total)
        base = round(100 * ratio)
        if green == total and total > 0:
            base = 100
        base = max(50, base)
    return _clamp_score(base)


async def get_user_score(uid: int) -> int:
    rows = await _events_in_window(uid, _WINDOW_SECONDS)
    # exclude manual_adjust points from events to avoid double-counting (manual delta is stored separately)
    total_points = 0
    for row in rows:
        kind = str(row["kind"] or "")
        if kind == "manual_adjust":
            continue
        total_points += int(row["points"] or 0)
    manual = await _manual_delta(uid)
    base = await _baseline(uid)
    return _clamp_score(base + total_points + manual)


async def get_scores(uids: List[int]) -> Dict[int, int]:
    results: Dict[int, int] = {}
    for raw_uid in uids:
        results[int(raw_uid)] = await get_user_score(int(raw_uid))
    return results


async def get_user_stats(uid: int, days: int = 30) -> Dict[str, Any]:
    window_seconds = max(1, int(days) * 86400)
    rows = await _events_in_window(uid, window_seconds)
    groups: Dict[str, int] = defaultdict(int)
    events_total = 0
    items: List[Dict[str, Any]] = []
    for row in rows:
        kind = str(row["kind"])
        points = int(row["points"])
        # do not add manual_adjust points from events into totals/groups — authoritative source is leader_rating_adjust
        if kind != "manual_adjust":
            events_total += points
            group = _EVENT_GROUPS.get(kind, "other")
            groups[group] += points
        items.append(
            {
                "kind": kind,
                "points": points,
                "when_ts": int(row["when_ts"]),
                "meta": _decode_meta(row["meta"]),
                "poll_id": row["poll_id"],
                "deal_id": row["deal_id"],
            }
        )
    manual = await _manual_delta(uid)
    base = await _baseline(uid)
    # ensure manual adjustments are reflected in groups under 'manual'
    if manual:
        groups["manual"] = int(groups.get("manual", 0)) + int(manual)
    score = _clamp_score(base + events_total + manual)
    return {
        "uid": int(uid),
        "baseline": base,
        "window_days": int(days),
        "events_total": events_total,
        "manual_delta": manual,
        "score": score,
        "groups": dict(groups),
        "events": items,
    }


async def get_cant_work_weeks(uid: Optional[int] = None) -> Dict[int, List[str]]:
    await init_db()
    query = "SELECT uid, week_iso FROM leader_cantwork_weeks"
    params: Tuple[Any, ...] = tuple()
    if uid is not None:
        query += " WHERE uid = ?"
        params = (int(uid),)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    result: Dict[int, List[str]] = defaultdict(list)
    for row in rows:
        result[int(row["uid"] or 0)].append(str(row["week_iso"]))
    for key, value in result.items():
        value.sort()
        result[key] = value
    return dict(result)


async def recompute_baseline_for_all() -> None:
    return None


async def _test() -> None:
    assert _bucket_reply(6) == "d12"
    assert _bucket_reply(20) == "d36"
    assert _bucket_reply(60) == "late"
    assert _bucket_confirm(12) == "d24"
    assert _bucket_confirm(30) == "d36"
    assert _bucket_confirm(50) == "late"
    assert _clamp_score(-5) == max(_CAP_MIN, 0)
    assert _clamp_score(150) == min(_CAP_MAX, 150)

    async def _fake_snapshot(_uid: int) -> Dict[str, int]:
        return {"green": 1, "yellow": 1, "red": 0, "total": 2}

    original_snapshot = globals().get("get_user_traffic_light")
    globals()["get_user_traffic_light"] = _fake_snapshot  # type: ignore[assignment]

    test_uid = 998001
    now_ts = _now_ts()
    await init_db()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("DELETE FROM leader_rating_events WHERE uid = ?", (test_uid,))
        await db.execute("DELETE FROM leader_rating_adjust WHERE uid = ?", (test_uid,))
        await db.execute("DELETE FROM leader_rating_flags WHERE uid = ?", (test_uid,))
        await db.execute("DELETE FROM leader_cantwork_weeks WHERE uid = ?", (test_uid,))
        await db.commit()

    await record_event(test_uid, "poll_reply", {"t_open": now_ts - 3600}, when=now_ts, poll_id="p1")
    await record_event(test_uid, "confirm", {"t_assign": now_ts - 1200}, when=now_ts, deal_id="d1")
    await record_event(test_uid, "game_main", {"deal_id": "d1"}, when=now_ts, deal_id="d1")
    await record_event(test_uid, "urgent_replacement", {"deal_id": "d1"}, when=now_ts, deal_id="d1")
    await adjust_score(test_uid, 5, "test-adjust")

    score = await get_user_score(test_uid)
    stats = await get_user_stats(test_uid)
    assert score >= 0
    assert stats["groups"].get("urgent", 0) >= int(_weight("urgent_replacement"))
    assert stats["manual_delta"] >= 5

    globals()["get_user_traffic_light"] = original_snapshot  # type: ignore[assignment]
    print("services/ratings.py ✅ tests passed")


if __name__ == "__main__":
    asyncio.run(_test())

# 2025-09-17 · модуль рейтинга: выровнено под SSOT.

# 2025-09-17 · модуль рейтинга: выровнено под SSOT.





