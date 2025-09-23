#!/usr/bin/env python3
"""
Тест исправлений для polls_lifecycle.py
Проверяет основные исправления без запуска полного бота.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def test_imports():
    """Тест 1: Проверка импортов и отсутствия ImportError"""
    print("Тест 1: Проверка импортов...")
    
    try:
        # Проверяем что можно импортировать основные функции
        from handlers.polls_lifecycle import (
            swap_request_handler,
            swap_accept_handler, 
            swap_decline_handler,
            swap_cancel_handler,
            swap_request_menu_handler,
            invalidate_svetofor_cache,
            _compose_tag,
            _sv_cache_get,
            _sv_cache_set
        )
        print("OK Все функции успешно импортированы")
        return True
    except ImportError as e:
        print(f"FAIL Ошибка импорта: {e}")
        return False

def test_compose_tag():
    """Тест 2: Проверка исправления двойных точек"""
    print("Тест 2: Проверка _compose_tag...")
    
    from handlers.polls_lifecycle import _compose_tag
    
    # Тест с двойными точками
    result1 = _compose_tag("Иван И..", "1")
    expected1 = "иван и.1"
    
    # Тест с одной точкой
    result2 = _compose_tag("Петр П.", "2") 
    expected2 = "петр п.2"
    
    # Тест без точек
    result3 = _compose_tag("Анна А", "Адм")
    expected3 = "анна а.адм"
    
    success = (
        result1 == expected1 and
        result2 == expected2 and  
        result3 == expected3
    )
    
    if success:
        print("✅ _compose_tag работает корректно")
        print(f"   'Иван И..' + '1' = '{result1}'")
        print(f"   'Петр П.' + '2' = '{result2}'") 
        print(f"   'Анна А' + 'Адм' = '{result3}'")
    else:
        print("❌ _compose_tag работает некорректно")
        print(f"   Ожидалось: {expected1}, получено: {result1}")
        print(f"   Ожидалось: {expected2}, получено: {result2}")
        print(f"   Ожидалось: {expected3}, получено: {result3}")
    
    return success

def test_svetofor_cache():
    """Тест 3: Проверка кэша светофора"""
    print("🔍 Тест 3: Проверка кэша светофора...")
    
    from handlers.polls_lifecycle import _sv_cache_set, _sv_cache_get, invalidate_svetofor_cache
    
    # Очищаем кэш
    invalidate_svetofor_cache()
    
    # Тестируем установку и получение
    _sv_cache_set(123, "Тестовая игра", "green")
    result = _sv_cache_get(123, "Тестовая игра")
    
    if result == "green":
        print("✅ Кэш светофора работает корректно")
        print(f"   Сохранено и получено: {result}")
        
        # Тестируем очистку
        invalidate_svetofor_cache(123, "Тестовая игра")
        result_after_clear = _sv_cache_get(123, "Тестовая игра")
        
        if result_after_clear is None:
            print("✅ Очистка кэша работает корректно")
            return True
        else:
            print("❌ Очистка кэша не работает")
            return False
    else:
        print(f"❌ Кэш светофора не работает. Ожидалось: 'green', получено: {result}")
        return False

def test_smoke():
    """Тест 4: Smoke-тест основных функций"""
    print("🔍 Тест 4: Smoke-тест...")
    
    try:
        from handlers.polls_lifecycle import _test
        _test()
        print("✅ Smoke-тесты прошли успешно")
        return True
    except Exception as e:
        print(f"❌ Smoke-тесты не прошли: {e}")
        return False

def main():
    """Запуск всех тестов"""
    print("Запуск тестов исправлений polls_lifecycle.py\n")
    
    tests = [
        test_imports,
        test_compose_tag, 
        test_svetofor_cache,
        test_smoke
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"FAIL Тест {test.__name__} упал с ошибкой: {e}")
            results.append(False)
        print()
    
    # Итоги
    passed = sum(results)
    total = len(results)
    
    print("=" * 50)
    print(f"Результаты: {passed}/{total} тестов прошли")
    
    if passed == total:
        print("Все исправления работают корректно!")
        return 0
    else:
        print("Некоторые исправления требуют доработки")
        return 1

if __name__ == "__main__":
    exit(main())