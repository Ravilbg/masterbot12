# -*- coding: utf-8 -*-
from pathlib import Path

new_content = '''import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.anyio("asyncio")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import handlers.polls_lifecycle as polls_lifecycle
import handlers.confirmations as confirmations
from handlers import my_games
from core.state import state
from core.config import settings
from services import amocrm as amo_module


class DummyBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": kwargs.get("reply_markup"),
        }
        self.sent_messages.append(payload)
        return SimpleNamespace(message_id=len(self.sent_messages), text=text, reply_markup=kwargs.get("reply_markup"))


@pytest.fixture(autouse=True)
def reset_state():
    attrs = [
        "locked_distribution",
        "distribution_cache",
        "swap_requests",
        "pending_confirmations",
        "swap_replacements",
        "user_short",
        "current_poll_deals",
        "games_by_user",
        "assigned_index",
        "respondents_cache",
        "_all_confirmed_announced",
    ]
    snapshot = {name: getattr(state, name, None) for name in attrs}
    try:
        state.locked_distribution = {}
        state.distribution_cache = {}
        state.swap_requests = {}
        state.pending_confirmations = {}
        state.swap_replacements = {}
        state.user_short = {}
        state.current_poll_deals = []
        state.games_by_user = {}
        state.assigned_index = {}
        state.respondents_cache = {}
        state._all_confirmed_announced = set()  # type: ignore[attr-defined]
        yield
    finally:
        for name, value in snapshot.items():
            setattr(state, name, value)


@pytest.fixture
def bot_env(monkeypatch):
    dummy_bot = DummyBot()

    def _get_current_cls(_cls):
        return dummy_bot

    monkeypatch.setattr(polls_lifecycle.Bot, "get_current", classmethod(_get_current_cls), raising=False)
    monkeypatch.setattr(my_games.Bot, "get_current", classmethod(_get_current_cls), raising=False)

    async def _noop(*_args, **_kwargs):
        return None

    async def _fake_short(uid):
        return state.user_short.get(uid, f"uid:{uid}")

    monkeypatch.setattr(polls_lifecycle, "short_name", _fake_short, raising=False)

    monkeypatch.setattr(polls_lifecycle, "_sync_leader_report", _noop, raising=False)
    monkeypatch.setattr(polls_lifecycle, "_check_ready_state", _noop, raising=False)
    monkeypatch.setattr(polls_lifecycle, "_refresh_detail_views", _noop, raising=False)

    async def _notify_stub(_bot=None):
        return 999

    monkeypatch.setattr(polls_lifecycle, "_notify_chat_id", _notify_stub, raising=False)
    monkeypatch.setattr(my_games, "resolve_notify_chat_id", lambda *a, **kw: 999)

    redraw_calls: list[int] = []

    async def _fake_redraw(uid):
        redraw_calls.append(uid)

    monkeypatch.setattr(polls_lifecycle, "_soft_redraw_my_games", _fake_redraw, raising=False)

    async def _fake_deal(deal_id):
        return {
            "id": deal_id,
            "game_name": "Test game",
            "event_date": "01.01",
            "event_time": "12:00",
            "package": "Package",
            "bonuses": "Bonus",
        }

    monkeypatch.setattr(amo_module, "get_deal_by_id", _fake_deal, raising=False)

    return dummy_bot, redraw_calls


class DummyCallback:
    def __init__(self, uid, data):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.answers: list[dict] = []
        self.message = SimpleNamespace(bot=None)

    async def answer(self, text, show_alert=False):
        self.answers.append({"text": text, "alert": show_alert})


def _base_setup(deal_id: int, initiator_uid: int) -> None:
    state.locked_distribution = {deal_id: {"lead1": f"Initiator I.1|{initiator_uid}"}}
    state.distribution_cache = {str(deal_id): {"lead1": f"Initiator I.1|{initiator_uid}"}}
    state.swap_requests = {deal_id: {"by": initiator_uid, "role": "main", "slot": "lead1", "accepted_by": None, "awaiting_confirmation": False}}
    state.user_short = {initiator_uid: "Initiator I."}
    state.current_poll_deals = [{
        "id": deal_id,
        "game_name": "Test game",
        "event_date": "01.01",
        "event_time": "12:00",
        "package": "Package",
        "bonuses": "Bonus",
    }]


def _set_status_map(monkeypatch, mapping):
    async def _status(uid, _game):
        return mapping.get(uid, "")

    monkeypatch.setattr(polls_lifecycle, "_sv_status", _status, raising=False)


async def test_yellow_cannot_replace_green_core_role(monkeypatch, bot_env):
    dummy_bot, _ = bot_env
    deal_id = 101
    initiator = 501
    candidate = 777
    _base_setup(deal_id, initiator)
    _set_status_map(monkeypatch, {candidate: "yellow", initiator: "green"})

    cb = DummyCallback(candidate, f"swap_accept_{deal_id}_main")
    await polls_lifecycle.swap_accept_handler(cb)

    assert cb.answers[-1]["text"] == "\U0001F512 Нельзя принять замену: ваш статус не позволяет занять эту роль."
    assert not dummy_bot.sent_messages
    assert state.distribution_cache[str(deal_id)]["lead1"].endswith(f"|{initiator}")


async def test_red_only_goes_to_trainee(monkeypatch, bot_env):
    dummy_bot, _ = bot_env
    deal_id = 102
    initiator = 601
    candidate = 888
    _base_setup(deal_id, initiator)
    _set_status_map(monkeypatch, {candidate: "red", initiator: "yellow"})

    cb = DummyCallback(candidate, f"swap_accept_{deal_id}_main")
    await polls_lifecycle.swap_accept_handler(cb)

    assert cb.answers[-1]["text"] == "\U0001F512 Нельзя принять замену: ваш статус не позволяет занять эту роль."
    assert not dummy_bot.sent_messages


async def test_green_accepts_and_updates_state(monkeypatch, bot_env):
    dummy_bot, redraw_calls = bot_env
    deal_id = 103
    initiator = 701
    candidate = 909
    _base_setup(deal_id, initiator)
    _set_status_map(monkeypatch, {candidate: "green", initiator: "yellow"})
    state.user_short[candidate] = "Candidate C."

    cb = DummyCallback(candidate, f"swap_accept_{deal_id}_main")
    await polls_lifecycle.swap_accept_handler(cb)

    label_cache = state.distribution_cache[str(deal_id)]["lead1"]
    label_locked = state.locked_distribution[deal_id]["lead1"]
    assert label_cache.endswith(f"|{candidate}")
    assert label_locked.endswith(f"|{candidate}")
    assert ".1" in label_cache

    pending = state.pending_confirmations[deal_id]["pending"]["main"]
    assert pending == {candidate}
    assert state.swap_requests[deal_id]["awaiting_confirmation"] is True
    assert cb.answers[-1]["text"] == "\u2705 Принято. Подтвердите участие в «Моих играх»."

    expected_message = "\n".join([
        "\u2705 Состав команды обновлён.",
        "\U0001F3AE «Test game» — 01.01 12:00",
        "• Candidate C.1 выходит на замену Initiator I.1",
        "",
        'Подтвердите участие в "Моих играх" чтобы проставить теги в CRM и завершить сделку.',
    ])
    assert dummy_bot.sent_messages[0]["text"] == expected_message
    assert candidate in redraw_calls and initiator in redraw_calls


async def test_final_announcement_uses_new_template(monkeypatch, bot_env):
    dummy_bot, _ = bot_env
    deal_id = 201
    candidate = 303
    partner = 404

    async def _always_confirmed(_deal_id):
        return True

    monkeypatch.setattr(confirmations, "_all_required_confirmed", _always_confirmed, raising=False)

    state.locked_distribution = {
        deal_id: {
            "lead1": "Candidate C.1|303",
            "assistant1": "Partner P.2|404",
        }
    }
    state.swap_replacements = {deal_id: {"candidate": candidate, "role": "main", "new_label": "Candidate C.1", "old_label": "Initiator I.1", "confirmed": False}}
    state.user_short = {candidate: "Candidate C.", partner: "Partner P."}
    state.current_poll_deals = [{
        "id": deal_id,
        "game_name": "Test game",
        "event_date": "01.01",
        "event_time": "12:00",
        "package": "Package",
        "bonuses": "Bonus",
    }]

    await my_games.announce_if_all_confirmed(deal_id)

    expected_lines = [
        "\u2705 Вся команда подтвердила участие.",
        "\U0001F389 Test game — 01.01 12:00 Package Bonus",
        "• Candidate C.1 Спасибо за отклик! \U0001F3A4",
        "• Partner P.2",
    ]
    assert dummy_bot.sent_messages[-1]["text"] == "\n".join(expected_lines)
    assert deal_id in state._all_confirmed_announced  # type: ignore[attr-defined]
    assert state.swap_replacements[deal_id]["confirmed"] is True
'''

Path('tests/test_swap_flow_rules.py').write_text(new_content, encoding='utf-8')
