# handlers/__init__.py
# -----------------------------------------------------------------------------
"""Регистрация Telegram-роутеров для aiogram.Dispatcher.
Вызывать только через handlers.setup(dispatcher).
"""

from aiogram import Dispatcher, Router as _AiogramRouter
import inspect

from .ratings          import router as ratings_router
from .ratings_admin    import router as ratings_admin_router
from .my_games         import router as my_games_router          # «Мои игры»
from .games            import router as games_router
from .polls_lifecycle  import router as polls_lifecycle_router
from .polls_distribution import router as polls_distribution_router
from .poll_details     import router as poll_details_router
from .confirmations    import router as confirmations_router
from .stats            import router as stats_router
from .swap import router as swap_router  # «🔁 Замена» из «Моих игр»
from .p2p_swap import router as p2p_swap_router

def setup(dp):
    """Подключает все доступные роутеры."""
    routers = [
        ratings_router,
        ratings_admin_router,
        my_games_router,          # «Мои игры» остаётся рядом с рейтингами
        games_router,
        polls_lifecycle_router,
        polls_distribution_router,
        poll_details_router,
        confirmations_router,
        stats_router,
        swap_router,
    ]
    for r in routers:
        dp.include_router(_as_router(r))


def _as_router(r):
    """Привести значение к aiogram.Router.

    Ожидается, что в проекте в переменных уже находятся объекты Router
    (см. imports выше). Но на случай несовпадений делаем безопасную
    проверку по типу и минимальный duck-typing, чтобы Pylance не ругался
    на неопределённый символ.
    """
    # Прямой экземпляр Router
    if isinstance(r, _AiogramRouter):
        return r
    # Duck-typing: объект имеет атрибуты, характерные для роутера
    if hasattr(r, "routes") or hasattr(r, "middleware"):
        return r
    raise TypeError(f"Expected aiogram.Router-like object, got: {type(r)!r}")
