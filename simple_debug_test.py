#!/usr/bin/env python3
"""Простой отладочный тест функции замены"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

# Добавляем корневую папку в путь
ROOT = Path(__file__).resolve().parents[0]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state import state
from handlers import polls_lifecycle
from handlers import my_games

async def simple_debug():
    """Простой отладочный тест"""
    print("Запуск простого теста замены...")
    
    # Настраиваем состояние
    deal_id = 42
    initiator_uid = 501
    candidate_uid = 777
    
    # Инициализируем состояние
    state.swap_open = {deal_id: {"by": initiator_uid, "role": "main"}}
    state.locked_distribution = {deal_id: {"lead1": f"Initiator|{initiator_uid}"}}
    state.distribution_cache = {str(deal_id): {"lead1": f"Initiator|{initiator_uid}"}}
    state.user_short = {initiator_uid: "Initiator", candidate_uid: "Candidate"}
    
    print("Начальное состояние:")
    print(f"  swap_open: {state.swap_open}")
    print(f"  locked_distribution: {state.locked_distribution}")
    
    # Проверяем _is_swap_pending
    try:
        is_pending = my_games._is_swap_pending(deal_id, initiator_uid)
        print(f"_is_swap_pending результат: {is_pending}")
    except Exception as e:
        print(f"Ошибка _is_swap_pending: {e}")
    
    # Проверяем _remove_uid_from_dist
    try:
        test_dist = {"lead1": f"User|{initiator_uid}", "lead2": "Other|999"}
        print(f"До удаления: {test_dist}")
        polls_lifecycle._remove_uid_from_dist(test_dist, initiator_uid)
        print(f"После удаления: {test_dist}")
    except Exception as e:
        print(f"Ошибка _remove_uid_from_dist: {e}")
    
    # Проверяем _deal_lock
    try:
        lock = polls_lifecycle._deal_lock(deal_id)
        print(f"Лок создан: {type(lock)}")
        
        async with lock:
            print("Лок работает корректно")
    except Exception as e:
        print(f"Ошибка _deal_lock: {e}")
    
    print("Тест завершен!")

if __name__ == "__main__":
    asyncio.run(simple_debug())