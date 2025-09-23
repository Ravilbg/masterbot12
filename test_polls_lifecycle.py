#!/usr/bin/env python3
import asyncio
import sys
import os
from datetime import datetime

# ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def _async_test_deduplication():
    from handlers.polls_lifecycle import _event_dt, _has_leader_tags, _is_preliminary_status, _check_ready_state

    assert _event_dt({"event_datetime": datetime.now()}) is not None
    assert _has_leader_tags({"tags": [{"name": "Иван И.1"}]}) is True
    assert _has_leader_tags({"tags": []}) is False
    assert _is_preliminary_status({"status_name": "Предварительная заявка"}) is True
    assert _is_preliminary_status({"status_name": "Бронь"}) is False

    # ensure _check_ready_state runs without raising
    await _check_ready_state([1, 2, 3])


def test_deduplication():
    asyncio.run(_async_test_deduplication())


#!/usr/bin/env python3
"""
Мини-тест для проверки дедупликации хелперов в polls_lifecycle.py
"""

import asyncio
import sys
import os
from datetime import datetime

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def _async_test_deduplication():
    """Тестирует дедупликацию хелперов"""
    # Небольшой smoke-тест для импортируемых хелперов
    from handlers.polls_lifecycle import _event_dt, _has_leader_tags, _is_preliminary_status
    from handlers.polls_lifecycle import _sync_leader_report, _check_ready_state

    # basic assertions
    assert _event_dt({"event_datetime": datetime.now()}) is not None
    assert _has_leader_tags({"tags": [{"name": "Иван И.1"}]}) is True
    assert _has_leader_tags({"tags": []}) is False
    assert _is_preliminary_status({"status_name": "Предварительная заявка"}) is True
    assert _is_preliminary_status({"status_name": "Бронь"}) is False

    # call check_ready_state to ensure it runs without crashing
    await _check_ready_state([1, 2, 3])


def test_deduplication():
    """Pytest wrapper that runs the async smoke-test."""
    return asyncio.run(_async_test_deduplication())