# services/cache.py
# ─────────────────────────────────────────────────────────────────────────────
# Простой in-memory кеш с TTL, полностью заменяет Redis-клиент.
# Если нужен сброс — вызывайте clear().
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import json
from typing import Any, Callable, Optional, TypeVar, Awaitable, Dict
from datetime import datetime, timedelta

from core.config import settings

logger = logging.getLogger(__name__)
T = TypeVar("T")


class InMemoryCache:
    """
    Cache-класс, хранящий пары {key: (value: str, expires: datetime)}.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    async def get(self, key: str) -> Optional[str]:
        entry = self._store.get(key)
        if not entry:
            return None
        if datetime.now() >= entry["expires"]:
            # срок истёк
            del self._store[key]
            return None
        return entry["value"]

    async def set(self, key: str, value: str, ex: int) -> None:
        self._store[key] = {
            "value": value,
            "expires": datetime.now() + timedelta(seconds=ex),
        }

    async def clear(self) -> None:
        """Полностью очищает кеш."""
        self._store.clear()
        logger.info("[cache] In-memory cache cleared")

    async def remember(
        self,
        key: str,
        ex: int,
        fetcher: Callable[[], Awaitable[T]],
    ) -> T:
        """
        Возвращает распарсенный JSON из кеша или выполняет fetcher().
        После fetcher() сохраняет результат в кеш на ex секунд.
        """
        raw = await self.get(key)
        if raw is not None:
            try:
                return json.loads(raw)
            except Exception:
                # если что-то пошло не так — игнорируем кеш
                pass

        # вызываем источник
        result = await fetcher()

        # пытаемся сохранить
        try:
            payload = json.dumps(result, ensure_ascii=False, default=str)
            await self.set(key, payload, ex)
        except Exception as exc:
            logger.debug("[cache] REMEMBER %s failed to set: %s", key, exc)

        return result


# глобальный инстанс для всего проекта
redis_cache = InMemoryCache()


async def _test():
    """Встроенный тест InMemoryCache."""
    # пишем, сразу читаем
    await redis_cache.set("foo", "bar", ex=1)
    assert await redis_cache.get("foo") == "bar"
    # ждём истечения срока
    import asyncio as _a; await _a.sleep(1.1)
    assert await redis_cache.get("foo") is None

    # тест remember
    async def fetch(): return {"x": 1}
    val = await redis_cache.remember("key", ex=1, fetcher=fetch)
    assert val == {"x": 1}
    print("cache tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
