# handlers/__init__.py
# -----------------------------------------------------------------------------
"""Регистрация Telegram-роутеров для aiogram.Dispatcher.
Вызывать только через handlers.setup(dispatcher).
"""

from aiogram import Dispatcher

from .ratings          import router as ratings_router
from .ratings_admin    import router as ratings_admin_router
from .my_games         import router as my_games_router          # «Мои игры»
from .games            import router as games_router
from .polls_lifecycle  import router as polls_lifecycle_router
from .polls_distribution import router as polls_distribution_router
from .poll_details     import router as poll_details_router
from .confirmations    import router as confirmations_router
from .stats            import router as stats_router

__all__ = [
    "ratings_router",
    "ratings_admin_router",
    "my_games_router",
    "games_router",
    "polls_lifecycle_router",
    "polls_distribution_router",
    "poll_details_router",
    "confirmations_router",
    "stats_router",
    "setup",
]


def setup(dp: Dispatcher) -> None:
    """Подключает все доступные роутеры."""
    for r in (
        ratings_router,
        ratings_admin_router,
        my_games_router,          # «Мои игры» остаётся рядом с рейтингами
        games_router,
        polls_lifecycle_router,
        polls_distribution_router,
        poll_details_router,
        confirmations_router,
        stats_router,
    ):
        dp.include_router(r)
