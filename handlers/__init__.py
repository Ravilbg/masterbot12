"""
Регистрация всех Telegram‑роутеров для aiogram.Dispatcher.
Импортируйте пакет и вызовите setup(dispatcher).
"""

from aiogram import Router

from .games import router as games_router
from .polls import router as polls_router
from .poll_details import router as poll_details_router

def setup(dispatcher):
    """Подключает все роутеры к диспетчеру."""
    for r in (games_router, polls_router, poll_details_router):
        dispatcher.include_router(r)