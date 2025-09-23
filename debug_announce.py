# -*- coding: utf-8 -*-
import asyncio
from handlers import my_games
from core.config import settings
from core.state import state

async def _always_confirmed(_did):
    return True

my_games._all_required_confirmed = _always_confirmed
state._all_confirmed_announced = set()
state.locked_distribution = {
    201: {
        "lead1": "Кандидат К.1|303",
        "assistant1": "Помощник П.2|404",
    }
}
state.swap_replacements = {201: {"candidate": 303, "role": "main", "new_label": "Кандидат К.1", "old_label": "Инициатор И.1", "confirmed": False}}
state.user_short = {303: "Кандидат К.", 404: "Помощник П."}
state.current_poll_deals = [{
    "id": 201,
    "game_name": "Test game",
    "event_date": "01.01",
    "event_time": "12:00",
    "package": "Премиум",
    "bonuses": "Фото",
}]

class DummyBot:
    def __init__(self):
        self.sent_messages = []
    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))
        return None

dummy_bot = DummyBot()
my_games.Bot.get_current = staticmethod(lambda: dummy_bot)
my_games.resolve_notify_chat_id = lambda *a, **k: 999

async def main():
    await my_games.announce_if_all_confirmed(201)
    print(dummy_bot.sent_messages)

asyncio.run(main())
