#!/usr/bin/env python3
"""Минимальный тест для отладки функции замены"""

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

class DummyCallback:
    def __init__(self, uid, data):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.answers = []

    async def answer(self, text, show_alert=False):
        self.answers.append({"text": text, "alert": show_alert})
        print(f"[CALLBACK] {text} (alert={show_alert})")

async def debug_minimal_swap():
    """Минимальный тест функции замены"""
    print("[DEBUG] Минимальный тест функции замены...")
    
    # Настраиваем состояние
    deal_id = 42
    initiator_uid = 501
    candidate_uid = 777
    
    # Инициализируем состояние
    state.swap_open = {deal_id: {"by": initiator_uid, "role": "main"}}
    state.locked_distribution = {deal_id: {"lead1": f"Initiator|{initiator_uid}"}}
    state.distribution_cache = {str(deal_id): {"lead1": f"Initiator|{initiator_uid}"}}
    state.user_short = {initiator_uid: "Initiator", candidate_uid: "Candidate"}
    
    print(f"[BEFORE] locked_distribution: {state.locked_distribution}")
    print(f"[BEFORE] distribution_cache: {state.distribution_cache}")
    print(f"[BEFORE] swap_open: {state.swap_open}")
    
    # Создаем callback
    cb = DummyCallback(candidate_uid, f"swap_accept_{deal_id}_main")
    
    # Выполняем accept
    try:
        await polls_lifecycle.swap_accept_handler(cb)
    except Exception as e:
        print(f"[ERROR] Ошибка в swap_accept_handler: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"[AFTER] locked_distribution: {state.locked_distribution}")
    print(f"[AFTER] distribution_cache: {state.distribution_cache}")
    print(f"[AFTER] swap_open: {state.swap_open}")
    print(f"[AFTER] callback answers: {cb.answers}")
    
    print("\n[DONE] Минимальный тест завершен!")

if __name__ == "__main__":
    asyncio.run(debug_minimal_swap())