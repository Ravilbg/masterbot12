# init_performance.py - Инициализация оптимизаций производительности

import logging

logger = logging.getLogger(__name__)

def init_performance_optimizations():
    """
    Инициализирует оптимизации производительности для всего проекта.
    Должна вызываться при старте приложения.
    """
    try:
        # Применяем оптимизации к poll_details
        from performance_fixes import apply_performance_fixes
        apply_performance_fixes()
        
        logger.info("✅ Оптимизации производительности успешно инициализированы")
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации оптимизаций: {e}")

if __name__ == "__main__":
    init_performance_optimizations()