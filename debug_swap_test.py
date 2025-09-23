#!/usr/bin/env python3
"""Отладочный тест функции замены"""

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

class DummyBot:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, chat_id, text, **kwargs):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": kwargs.get("reply_markup"),
        }
        self.sent_messages.append(payload)
        return SimpleNamespace(message_id=len(self.sent_messages), text=text)

class DummyCallback:
    def __init__(self, uid, data):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.answers = []

    async def answer(self, text, show_alert=False):
        self.answers.append({"text": text, "alert": show_alert})

async def debug_swap_functionality():
    """Отладочный тест функциональности замены"""
    print("[DEBUG] Отладка функциональности замены...")
    
    # Настраиваем состояние
    deal_id = 42
    initiator_uid = 501
    candidate_uid = 777
    
    # Инициализируем состояние
    state.swap_open = {deal_id: {"by": initiator_uid, "role": "main"}}
    state.locked_distribution = {deal_id: {"lead1": f"Initiator|{initiator_uid}"}}
    state.distribution_cache = {str(deal_id): {"lead1": f"Initiator|{initiator_uid}"}}
    state.user_short = {initiator_uid: "Initiator", candidate_uid: "Candidate"}
    
    print(f"[STATE] Начальное состояние:")
    print(f"   swap_open: {state.swap_open}")
    print(f"   locked_distribution: {state.locked_distribution}")
    print(f"   distribution_cache: {state.distribution_cache}")
    
    # Проверяем функцию _is_swap_pending
    try:
        is_pending_before = my_games._is_swap_pending(deal_id, initiator_uid)
        print(f"[OK] _is_swap_pending({deal_id}, {initiator_uid}) = {is_pending_before}")
    except Exception as e:
        print(f"[ERROR] Ошибка в _is_swap_pending: {e}")
    
    # Проверяем функцию _remove_uid_from_dist
    try:
        test_dist = {"lead1": f"User|{initiator_uid}", "lead2": "Other|999"}
        print(f"[BEFORE] До _remove_uid_from_dist: {test_dist}")
        polls_lifecycle._remove_uid_from_dist(test_dist, initiator_uid)
        print(f"[AFTER] После _remove_uid_from_dist: {test_dist}")
    except Exception as e:
        print(f"[ERROR] Ошибка в _remove_uid_from_dist: {e}")
    
    # Проверяем функцию _deal_lock
    try:
        lock = polls_lifecycle._deal_lock(deal_id)
        print(f"[LOCK] _deal_lock({deal_id}) создан: {type(lock)}")
        
        async with lock:
            print("[LOCK] Лок успешно захвачен и освобожден")
    except Exception as e:
        print(f"[ERROR] Ошибка с _deal_lock: {e}")
    
    # Тестируем swap_accept_handler
    try:
        # Подменяем Bot.get_current()
        dummy_bot = DummyBot()
        original_get_current = getattr(polls_lifecycle.Bot, "get_current", None)
        polls_lifecycle.Bot.get_current = staticmethod(lambda: dummy_bot)
        
        # Подменяем другие функции
        async def _fake_short(uid):
            return state.user_short.get(uid, f"User{uid}")
        
        async def _noop(*args, **kwargs):
            return None
        
        def _notify_stub(*args, **kwargs):
            return 999
        
        async def _fake_deal(deal_id):
            return {
                "id": deal_id,
                "game_name": "Test Game",
                "event_date": "01.01",
                "event_time": "12:00",
                "package": "Test Package",
            }
        
        polls_lifecycle.short_name = _fake_short
        polls_lifecycle._sync_leader_report = _noop
        polls_lifecycle._refresh_detail_views = _noop
        polls_lifecycle.resolve_notify_chat_id = _notify_stub
        
        # Подменяем get_deal_by_id
        import services.amocrm as amocrm
        amocrm.get_deal_by_id = _fake_deal
        
        print(f"[TEST] Тестируем swap_accept_handler...")
        
        # Создаем callback
        cb = DummyCallback(candidate_uid, f"swap_accept_{deal_id}_main")
        
        # Выполняем accept
        await polls_lifecycle.swap_accept_handler(cb)
        
        print(f"[RESULTS] Результаты:")
        print(f"   Ответы callback: {cb.answers}")
        print(f"   Отправленные сообщения: {len(dummy_bot.sent_messages)}")
        if dummy_bot.sent_messages:
            print(f"   Первое сообщение: {dummy_bot.sent_messages[0]['text'][:100]}...")
        
        # Проверяем состояние после accept
        print(f"[STATE] Состояние после accept:")
        print(f"   swap_open: {state.swap_open}")
        print(f"   locked_distribution: {state.locked_distribution}")
        print(f"   distribution_cache: {state.distribution_cache}")
        
        # Восстанавливаем оригинальные функции
        if original_get_current:
            polls_lifecycle.Bot.get_current = original_get_current
        
    except Exception as e:
        print(f"[ERROR] Ошибка в swap_accept_handler: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n[DONE] Отладка завершена!")

if __name__ == "__main__":
    asyncio.run(debug_swap_functionality())