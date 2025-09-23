#!/usr/bin/env python3
"""Простой тест функциональности замены"""

import asyncio
import sys
from pathlib import Path

# Добавляем корневую папку в путь
ROOT = Path(__file__).resolve().parents[0]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state import state
from handlers import polls_lifecycle
from handlers import my_games

async def test_swap_functionality():
    """Простой тест функциональности замены"""
    print("Тестируем функциональность замены...")
    
    # Настраиваем состояние
    deal_id = 42
    initiator_uid = 501
    candidate_uid = 777
    
    # Инициализируем состояние
    state.swap_open = {deal_id: {"by": initiator_uid, "role": "main"}}
    state.locked_distribution = {deal_id: {"lead1": f"Initiator|{initiator_uid}"}}
    state.distribution_cache = {str(deal_id): {"lead1": f"Initiator|{initiator_uid}"}}
    state.user_short = {initiator_uid: "Initiator", candidate_uid: "Candidate"}
    
    print(f"Начальное состояние:")
    print(f"   swap_open: {state.swap_open}")
    print(f"   locked_distribution: {state.locked_distribution}")
    print(f"   distribution_cache: {state.distribution_cache}")
    
    # Проверяем функцию _is_swap_pending
    is_pending_before = my_games._is_swap_pending(deal_id, initiator_uid)
    print(f"_is_swap_pending({deal_id}, {initiator_uid}) = {is_pending_before}")
    
    # Проверяем функцию _remove_uid_from_dist
    test_dist = {"lead1": f"User|{initiator_uid}", "lead2": "Other|999"}
    print(f"До _remove_uid_from_dist: {test_dist}")
    polls_lifecycle._remove_uid_from_dist(test_dist, initiator_uid)
    print(f"После _remove_uid_from_dist: {test_dist}")
    
    # Проверяем функцию _deal_lock
    try:
        lock = polls_lifecycle._deal_lock(deal_id)
        print(f"_deal_lock({deal_id}) создан: {type(lock)}")
        
        async with lock:
            print("Лок успешно захвачен и освобожден")
    except Exception as e:
        print(f"Ошибка с _deal_lock: {e}")
    
    print("\nТест завершен!")

if __name__ == "__main__":
    asyncio.run(test_swap_functionality())