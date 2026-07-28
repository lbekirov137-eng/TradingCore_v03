"""
Startup safety gate для облачного/любого запуска.

Читает ТОЛЬКО наличие/значения флагов режима из переменных окружения —
значения секретов (API-ключи и т.п.) здесь никогда не читаются и не
возвращаются. Если конфигурация небезопасна или режим не распознан,
процесс обязан НЕ запуститься (raise, а не тихий fallback).

Вызывается один раз при импорте api/server.py — до создания FastAPI
приложения, чтобы небезопасная конфигурация не позволила серверу
подняться вообще.
"""

import os

# LIVE сюда намеренно не входит — реальная торговля не поддерживается
# ни при каком значении TRADING_ENVIRONMENT.
VALID_ENVIRONMENTS = {"PAPER", "DEMO"}


class StartupSafetyError(RuntimeError):
    """Небезопасная или нераспознанная конфигурация — запуск запрещён."""


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_trading_environment() -> str:
    return os.getenv("TRADING_ENVIRONMENT", "PAPER").strip().upper()


def get_live_trading_flag() -> bool:
    return _env_flag("LIVE_TRADING", default=False)


def get_paper_trading_flag() -> bool:
    return _env_flag("PAPER_TRADING", default=True)


def get_demo_only_flag() -> bool:
    return _env_flag("DEMO_ONLY", default=True)


def build_startup_summary(environment: str = None) -> dict:
    """Безопасная сводка запуска. Значения секретов никогда не включаются."""

    environment = environment or get_trading_environment()

    return {
        "trading_environment": environment,
        "live_trading": get_live_trading_flag(),
        "paper_trading": get_paper_trading_flag(),
        "demo_only": get_demo_only_flag(),
        "leverage": 1,
        "live_order_code_present": False,
    }


def assert_safe_startup() -> dict:
    """
    Бросает StartupSafetyError, если конфигурация небезопасна:
      - LIVE_TRADING=true в любом виде;
      - TRADING_ENVIRONMENT=LIVE;
      - TRADING_ENVIRONMENT — нераспознанное значение.

    Возвращает безопасную сводку запуска при успехе.
    """

    environment = get_trading_environment()
    live_trading = get_live_trading_flag()

    if live_trading:
        raise StartupSafetyError(
            "LIVE_TRADING=true обнаружено в окружении — запуск запрещён. "
            "Реальная торговля заблокирована в этом репозитории (Gate G)."
        )

    if environment == "LIVE":
        raise StartupSafetyError(
            "TRADING_ENVIRONMENT=LIVE запрещён — запуск заблокирован (Gate G)."
        )

    if environment not in VALID_ENVIRONMENTS:
        raise StartupSafetyError(
            f"Нераспознанный TRADING_ENVIRONMENT='{environment}'. "
            f"Допустимые значения: {sorted(VALID_ENVIRONMENTS)}. Запуск заблокирован "
            f"(неизвестный режим не считается безопасным по умолчанию)."
        )

    return build_startup_summary(environment)


def runtime_safety_check() -> dict:
    """
    Повторная (не только при старте) проверка конфигурации. Правило:
    если сервис в рантайме обнаруживает попытку включить live-режим
    (например, переменные окружения изменены без пересоздания процесса)
    или нераспознанный режим — это трактуется как FAILED_SAFELY, а не
    игнорируется. Никогда не бросает исключение — вызывающий код решает,
    что делать с результатом (остановить цикл, вернуть 503 и т.п.).
    """
    try:
        assert_safe_startup()
        return {"safe": True, "reason": None}
    except StartupSafetyError as error:
        return {"safe": False, "reason": str(error)}
