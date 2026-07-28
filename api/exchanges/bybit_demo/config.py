"""
Конфигурация подключения к Bybit.

ЖЁСТКОЕ ПРАВИЛО БЕЗОПАСНОСТИ: при TRADING_ENVIRONMENT=DEMO разрешён
ТОЛЬКО официальный demo-эндпоинт Bybit. Любая попытка использовать
production-хост (api.bybit.com) в demo-режиме приводит к немедленной
ошибке конфигурации — подключение не создаётся.

Значения секретов НИКОГДА не логируются и не возвращаются наружу:
доступна только проверка "переменная задана / не задана".
"""

import os

DEMO_REST_URL = "https://api-demo.bybit.com"
DEMO_WS_PRIVATE_URL = "wss://stream-demo.bybit.com/v5/private"

# Публичный маркет-дата поток одинаков для demo и mainnet (только чтение).
PUBLIC_WS_URL = "wss://stream.bybit.com/v5/public/spot"

PRODUCTION_HOSTS = {
    "api.bybit.com",
    "api.bytick.com",
    "stream.bybit.com/v5/private",
}

ENV_DEMO = "DEMO"
ENV_PAPER = "PAPER"
ENV_LIVE = "LIVE"


class ConfigurationError(Exception):
    """Небезопасная или неполная конфигурация — подключение запрещено."""


def get_environment() -> str:
    return os.getenv("TRADING_ENVIRONMENT", ENV_PAPER).upper()


def credentials_present() -> dict:
    """
    Проверяет НАЛИЧИЕ переменных окружения, никогда не возвращая их значения.
    """
    return {
        "BYBIT_DEMO_API_KEY": bool(os.getenv("BYBIT_DEMO_API_KEY")),
        "BYBIT_DEMO_API_SECRET": bool(os.getenv("BYBIT_DEMO_API_SECRET")),
    }


def validate_endpoint(url: str, environment: str) -> None:
    """
    Отклоняет production-эндпоинты в DEMO-режиме.
    """

    if environment == ENV_DEMO:
        for production_host in PRODUCTION_HOSTS:
            if production_host in url:
                raise ConfigurationError(
                    f"Production-эндпоинт '{production_host}' запрещён в режиме DEMO. "
                    f"Разрешён только {DEMO_REST_URL}."
                )

        if not url.startswith(DEMO_REST_URL) and not url.startswith("wss://stream-demo."):
            raise ConfigurationError(
                f"В режиме DEMO разрешён только {DEMO_REST_URL} / stream-demo. Получено: {url}"
            )


def validate_demo_configuration() -> dict:
    """
    Полная проверка перед подключением. Возвращает отчёт без секретов.
    Бросает ConfigurationError, если конфигурация небезопасна.
    """

    environment = get_environment()

    if environment == ENV_LIVE:
        raise ConfigurationError(
            "TRADING_ENVIRONMENT=LIVE не поддерживается: реальная торговля "
            "заблокирована (см. AUTOTRADING_RELEASE_GATES.md, Gate G)."
        )

    if environment != ENV_DEMO:
        return {
            "environment": environment,
            "ready": False,
            "reason": f"Bybit Demo адаптер требует TRADING_ENVIRONMENT=DEMO (текущее: {environment}).",
            "credentials": credentials_present(),
        }

    present = credentials_present()
    missing = [name for name, ok in present.items() if not ok]

    validate_endpoint(DEMO_REST_URL, environment)

    if missing:
        return {
            "environment": environment,
            "ready": False,
            "reason": f"Отсутствуют переменные окружения: {', '.join(missing)}. Подключение не выполняется.",
            "credentials": present,
            "rest_url": DEMO_REST_URL,
        }

    return {
        "environment": environment,
        "ready": True,
        "reason": "Конфигурация DEMO корректна.",
        "credentials": present,
        "rest_url": DEMO_REST_URL,
    }
