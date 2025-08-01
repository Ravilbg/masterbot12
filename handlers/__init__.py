# handlers/__init__.py
# ────────────────────────────────────────────────────────────────────────────
"""
Регистрация всех Telegram-роутеров для aiogram.Dispatcher.
Импортируйте пакет и вызовите handlers.setup(dispatcher).
"""

from aiogram import Dispatcher

from .games import router   as games_router
from .polls_lifecycle import router   as polls_lifecycle_router
from .polls_distribution import router as polls_distribution_router
from .poll_details import router as poll_details_router
from .confirmations import router  as confirmations_router
from .stats import router   as stats_router

__all__ = [
    "games_router",
    "polls_lifecycle_router",
    "polls_distribution_router",
    "poll_details_router",
    "confirmations_router",
    "stats_router",
    "setup",
]

def setup(dp: Dispatcher) -> None:
    """Подключает все роутеры к диспетчеру."""
    for r in (
        games_router,
        polls_lifecycle_router,
        polls_distribution_router,
        poll_details_router,
        confirmations_router,
        stats_router,
    ):
        dp.include_router(r)
