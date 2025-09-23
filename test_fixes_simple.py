#!/usr/bin/env python3
"""
Простой тест исправлений для polls_lifecycle.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def test_imports():
    """Тест импортов"""
    print("Тест 1: Проверка импортов...")
    
    try:
        from handlers.polls_lifecycle import (
            swap_request_handler,
            swap_accept_handler, 
            swap_decline_handler,
            invalidate_svetofor_cache,
            _compose_tag
        )
        print("OK - Все функции успешно импортированы")
        return True
    except ImportError as e:
        print(f"FAIL - Ошибка импорта: {e}")
        return False

def test_compose_tag():
    """Тест исправления двойных точек"""
    print("Тест 2: Проверка _compose_tag...")
    
    from handlers.polls_lifecycle import _compose_tag
    
    result1 = _compose_tag("Иван И..", "1")
    result2 = _compose_tag("Петр П.", "2") 
    
    if "иван и.1" in result1.lower() and "петр п.2" in result2.lower():
        print("OK - _compose_tag работает корректно")
        return True
    else:
        print("FAIL - _compose_tag работает некорректно")
        return False

def test_svetofor_cache():
    """Тест кэша светофора"""
    print("Тест 3: Проверка кэша светофора...")
    
    try:
        from handlers.polls_lifecycle import _sv_cache_set, _sv_cache_get, invalidate_svetofor_cache
        
        invalidate_svetofor_cache()
        _sv_cache_set(123, "Тестовая игра", "green")
        result = _sv_cache_get(123, "Тестовая игра")
        
        if result == "green":
            print("OK - Кэш светофора работает")
            return True
        else:
            print("FAIL - Кэш светофора не работает")
            return False
    except Exception as e:
        print(f"FAIL - Ошибка кэша: {e}")
        return False

def main():
    """Запуск тестов"""
    print("Запуск тестов исправлений polls_lifecycle.py\n")
    
    tests = [test_imports, test_compose_tag, test_svetofor_cache]
    results = []
    
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"FAIL - Тест {test.__name__} упал: {e}")
            results.append(False)
        print()
    
    passed = sum(results)
    total = len(results)
    
    print("=" * 40)
    print(f"Результаты: {passed}/{total} тестов прошли")
    
    if passed == total:
        print("Все исправления работают!")
        return 0
    else:
        print("Некоторые исправления требуют доработки")
        return 1

if __name__ == "__main__":
    exit(main())