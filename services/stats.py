"""
services/stats.py — статистика игр по ведущим
─────────────────────────────────────────────────────────────────────────────
• games_per_leader(days=30) → Dict[int, int]
    Возвращает {user_id: кол-во сыгранных игр за последние *days* суток}.
• top_leaders(days=30, limit=10) → List[tuple[int, int]]
    Сортированный список (user_id, games) по убыванию.

Вся логика соответствует MasterBot Style Guide 12.92+:
async-first, короткие функции, встроенный _test().
"""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from pytz import timezone

from core.config import settings
from services.amocrm import get_amocrm_deals

# ███ [1.1] CONSTANTS & LOGGER
# --------------------------------------------------------------------
logger = logging.getLogger(__name__)
MSK_TZ = timezone("Europe/Moscow")


# ███ [2.0] PUBLIC API
# --------------------------------------------------------------------
async def games_per_leader(days: int = 30) -> Dict[int, int]:
    """
    Считает «сыгранные» игры (status == SUCCESSFUL_STATUS_ID) за *days* суток.

    Parameters
    ----------
    days : int
        Период в сутках, за который считаем статистику.

    Returns
    -------
    Dict[int, int]
        Mapping вида {user_id: games_count}.
    """
    deals = await get_amocrm_deals()
    if not deals:
        logger.debug("[stats] deals list empty → {}")
        return {}

    threshold = datetime.now(tz=MSK_TZ) - timedelta(days=days)
    ok_status = settings.SUCCESSFUL_STATUS_ID
    result: Dict[int, int] = {}

    for deal in deals:
        if deal.get("status_id") != ok_status:
            continue
        event_dt = deal.get("event_datetime")
        if not event_dt or event_dt < threshold:
            continue

        for lead in deal.get("team_leads", []):
            try:
                uid = int(lead.get("id", 0))
            except (TypeError, ValueError):
                continue
            result[uid] = result.get(uid, 0) + 1

    logger.debug("[stats] games_per_leader → %s", result)
    return result


async def top_leaders(days: int = 30, limit: int = 10) -> List[Tuple[int, int]]:
    """
    Возвращает топ-*limit* ведущих по числу игр за период.

    Сортировка по убыванию count, затем по возрастанию user_id.
    """
    stats = await games_per_leader(days)
    sorted_pairs = sorted(stats.items(), key=lambda kv: (-kv[1], kv[0]))
    return sorted_pairs[:limit]


# ███ [3.0] TESTS
# --------------------------------------------------------------------
async def _test():
    """
    Псевдо-тест: вызываем функции, убеждаемся, что ошибку не бросают
    и результат — словарь/список.
    """
    d = await games_per_leader(1)
    assert isinstance(d, dict)
    t = await top_leaders(1, 5)
    assert isinstance(t, list) and all(isinstance(i, tuple) for i in t)
    print("services.stats tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
