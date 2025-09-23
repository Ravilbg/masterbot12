# performance_fixes.py - Оптимизации производительности для poll_details.py

import asyncio
import time
from typing import Dict, List, Set, Any, Optional, Tuple
from core.state import state
from core.utils import short_name as _short_name
from services.gsheets import get_user_status_from_svetofor

# Batch-кэширование статусов светофора
async def batch_status_cached(uids: List[int], game_name: str) -> Dict[int, str]:
    """
    Пакетное получение статусов светофора для списка пользователей.
    Возвращает словарь uid -> status.
    """
    if not uids or not game_name:
        return {}
    
    g = game_name.strip().lower()
    results = {}
    
    # Проверяем локальный кэш
    _local_status_cache = getattr(state, '_local_status_cache', {})
    _ttl = 60 * 60 * 4  # 4 часа
    now = time.time()
    
    uncached_uids = []
    for uid in uids:
        key = f"sv:{uid}:{g}"
        if key in _local_status_cache:
            status, ts = _local_status_cache[key]
            if (now - ts) < _ttl:
                results[uid] = status
                continue
        uncached_uids.append(uid)
    
    # Пакетно загружаем недостающие статусы
    if uncached_uids:
        tasks = []
        for uid in uncached_uids:
            tasks.append(get_user_status_from_svetofor(uid, game_name))
        
        try:
            statuses = await asyncio.gather(*tasks, return_exceptions=True)
            for uid, status in zip(uncached_uids, statuses):
                if isinstance(status, Exception):
                    status = ""
                else:
                    status = (status or "").strip().lower()
                
                results[uid] = status
                # Обновляем кэш
                key = f"sv:{uid}:{g}"
                _local_status_cache[key] = (status, now)
        except Exception:
            # Fallback - по одному
            for uid in uncached_uids:
                try:
                    status = await get_user_status_from_svetofor(uid, game_name)
                    status = (status or "").strip().lower()
                except Exception:
                    status = ""
                results[uid] = status
                key = f"sv:{uid}:{g}"
                _local_status_cache[key] = (status, now)
    
    return results

# Batch-кэширование имён пользователей
async def batch_short_names(uids: List[int]) -> Dict[int, str]:
    """
    Пакетное получение коротких имён пользователей.
    Возвращает словарь uid -> short_name.
    """
    if not uids:
        return {}
    
    results = {}
    
    # Проверяем кэш в state
    cached_names = getattr(state, "user_short", {}) or {}
    uncached_uids = []
    
    for uid in uids:
        if uid in cached_names:
            results[uid] = cached_names[uid]
        else:
            uncached_uids.append(uid)
    
    # Пакетно загружаем недостающие имена
    if uncached_uids:
        tasks = []
        for uid in uncached_uids:
            tasks.append(_short_name(uid))
        
        try:
            names = await asyncio.gather(*tasks, return_exceptions=True)
            for uid, name in zip(uncached_uids, names):
                if isinstance(name, Exception):
                    name = f"uid:{uid}"
                else:
                    name = str(name or f"uid:{uid}").strip()
                
                results[uid] = name
                # Обновляем кэш
                if not hasattr(state, "user_short"):
                    state.user_short = {}
                state.user_short[uid] = name
        except Exception:
            # Fallback - по одному
            for uid in uncached_uids:
                try:
                    name = await _short_name(uid)
                    name = str(name or f"uid:{uid}").strip()
                except Exception:
                    name = f"uid:{uid}"
                results[uid] = name
                if not hasattr(state, "user_short"):
                    state.user_short = {}
                state.user_short[uid] = name
    
    return results

# Оптимизированная функция для получения всех участников
async def get_all_participants(deal_id: int, game_name: str) -> Dict[str, Any]:
    """
    Получает всех участников сделки с их статусами и именами за один проход.
    Возвращает словарь с предзагруженными данными.
    """
    # Собираем всех уникальных пользователей
    all_uids: Set[int] = set()
    
    # Из распределения
    dist = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id), {})
    for value in dist.values():
        if value:
            from handlers.poll_details import _parse_uid
            uid = _parse_uid(value)
            if uid:
                all_uids.add(uid)
    
    # Из откликнувшихся
    try:
        from handlers.poll_details import _get_respondents
        respondents = await _get_respondents(deal_id)
        all_uids.update(respondents.keys())
    except Exception:
        respondents = {}
    
    all_uids_list = list(all_uids)
    
    # Пакетно загружаем статусы и имена
    statuses_task = batch_status_cached(all_uids_list, game_name)
    names_task = batch_short_names(all_uids_list)
    
    statuses, names = await asyncio.gather(statuses_task, names_task)
    
    return {
        "statuses": statuses,
        "names": names,
        "respondents": respondents,
        "all_uids": all_uids
    }

# Оптимизированный пылесос
async def optimized_vacuum(uid: int, keep: Optional[List[int]] = None, bot=None) -> None:
    """
    Оптимизированный пылесос - делает только один вызов вместо двух.
    """
    keep = keep or []
    
    try:
        from core.utils import vacuum_private
        await vacuum_private(uid, keep=keep)
    except Exception:
        # Fallback на старую функцию
        try:
            from core.utils import delete_previous_private_messages
            if bot is not None:
                await delete_previous_private_messages(bot, uid, keep=keep)
            else:
                await delete_previous_private_messages(uid)
        except Exception:
            pass

# Функция для применения оптимизаций
def apply_performance_fixes():
    """
    Применяет оптимизации производительности к модулю poll_details.
    """
    try:
        import handlers.poll_details as pd
        
        # Заменяем функции на оптимизированные версии
        pd.batch_status_cached = batch_status_cached
        pd.batch_short_names = batch_short_names
        pd.get_all_participants = get_all_participants
        pd.optimized_vacuum = optimized_vacuum
        
        print("✅ Оптимизации производительности применены")
        
    except Exception as e:
        print(f"❌ Ошибка применения оптимизаций: {e}")

if __name__ == "__main__":
    apply_performance_fixes()