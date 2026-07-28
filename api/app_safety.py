"""
Единый startup safety gate и общие safety-эндпоинты для ВСЕХ entrypoint.

Зачем этот модуль существует. В репозитории два FastAPI-приложения:
api/server.py (облачный entrypoint) и api/main.py. Гейт безопасности жил
только в первом, поэтому запуск через `api.main:app` поднимал сервис
вообще без проверок: LIVE_TRADING=true не блокировался, /ready и /safety
отсутствовали, а /health отвечал "healthy" независимо от конфигурации.
Достаточно было указать другой модуль в start command, чтобы обойти всю
защиту.

Решение — вынести гейт, lifespan и safety-маршруты СЮДА и подключать их
в обоих приложениях. Вторая независимая реализация не создаётся: любое
изменение защиты автоматически действует на оба entrypoint.

Секреты здесь не читаются и не печатаются — только флаги режима.
"""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config.startup_safety import (
    StartupSafetyError,
    assert_safe_startup,
)


def enforce_startup_safety() -> dict[str, Any]:
    """
    Проверяет конфигурацию ДО создания приложения и до прослушивания порта.

    Небезопасная или нераспознанная конфигурация -> процесс не стартует:
      - LIVE_TRADING=true
      - TRADING_ENVIRONMENT=LIVE
      - TRADING_ENVIRONMENT с нераспознанным значением (fail-closed)

    Исключение намеренно пробрасывается: отказ импорта модуля — самый
    надёжный способ не дать сервису подняться.
    """
    try:
        summary = assert_safe_startup()
    except StartupSafetyError as error:
        print(
            f"[STARTUP SAFETY] REFUSED TO START: {error}",
            file=sys.stderr,
            flush=True,
        )
        raise

    print(
        f"[STARTUP SAFETY] OK: {summary}",
        file=sys.stderr,
        flush=True,
    )
    return summary


@asynccontextmanager
async def paper_monitor_lifespan(_app: FastAPI):
    """
    Управляет фоновым PAPER-монитором вместе с жизненным циклом сервера.

    Монитор выключен по умолчанию (PAPER_MONITOR_ENABLED); при выключенном
    флаге start() лишь фиксирует статус DISABLED и ничего не запускает.

    Отказ монитора намеренно НЕ роняет сервер: healthcheck смотрит на
    /health, и потеря наблюдательного цикла не должна приводить к
    рестарт-петле всего сервиса. Реальное состояние видно в /monitor/status.
    """
    from api.paper_monitor import paper_monitor

    try:
        paper_monitor.start()
    except Exception as error:  # pragma: no cover - защитный барьер
        print(
            f"[PAPER MONITOR] failed to start: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )

    try:
        yield
    finally:
        try:
            paper_monitor.stop()
        except Exception as error:  # pragma: no cover - защитный барьер
            print(
                f"[PAPER MONITOR] failed to stop cleanly: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )


def build_safety_summary() -> dict[str, Any]:
    """Сводка безопасности. Значения секретов никогда не выводятся."""
    from config.startup_safety import (
        get_demo_only_flag,
        get_live_trading_flag,
        get_paper_trading_flag,
        get_trading_environment,
    )
    from api.leverage_risk_engine import LeverageRiskEngine

    return {
        "trading_environment": get_trading_environment(),
        "paper_trading": get_paper_trading_flag(),
        "live_trading": get_live_trading_flag(),
        "demo_only": get_demo_only_flag(),
        "max_leverage": LeverageRiskEngine.MAX_LEVERAGE,
        "max_risk_percent": LeverageRiskEngine.MAX_RISK_PERCENT,
        "live_order_code_present": False,
    }


def build_health_payload() -> dict[str, Any]:
    """Состояние процесса. Никогда не содержит секретов."""
    from config.startup_safety import (
        get_demo_only_flag,
        get_live_trading_flag,
        get_paper_trading_flag,
    )

    return {
        "status": "HEALTHY",
        "mode": "PAPER",
        "paper_trading": get_paper_trading_flag(),
        "live_trading": get_live_trading_flag(),
        "demo_only": get_demo_only_flag(),
        "server_time": time.time(),
    }


def build_readiness_payload() -> tuple[int, dict[str, Any]]:
    """
    Readiness. В отличие от /health, активно ПЕРЕПРОВЕРЯЕТ конфигурацию
    в рантайме и отдаёт FAILED_SAFELY (503), если обнаружены:
      - попытка live-режима или нераспознанная конфигурация;
      - плечо выше разрешённого для PAPER-запуска (1x);
      - риск на сделку выше 0.1%.

    Правило: неизвестная конфигурация, попытка live-режима или
    повреждённое состояние => FAILED_SAFELY, а не молчаливая работа.
    """
    from config.startup_safety import runtime_safety_check
    from api.leverage_risk_engine import LeverageRiskEngine

    reasons: list[str] = []

    runtime_safety = runtime_safety_check()
    if not runtime_safety["safe"]:
        reasons.append(
            f"unsafe_configuration: {runtime_safety['reason']}"
        )

    if LeverageRiskEngine.MAX_LEVERAGE > 1:
        reasons.append(
            "leverage_above_paper_limit: MAX_LEVERAGE="
            f"{LeverageRiskEngine.MAX_LEVERAGE} (must be 1)"
        )

    if LeverageRiskEngine.MAX_RISK_PERCENT > 0.001:
        reasons.append(
            "risk_above_limit: MAX_RISK_PERCENT="
            f"{LeverageRiskEngine.MAX_RISK_PERCENT} (must be <= 0.001)"
        )

    ready = len(reasons) == 0

    return (
        200 if ready else 503,
        {
            "ready": ready,
            "status": "READY" if ready else "FAILED_SAFELY",
            "reasons": reasons,
        },
    )


def register_safety_routes(app: FastAPI) -> FastAPI:
    """
    Подключает /health, /ready, /safety и /monitor/status.

    Вызывается КАЖДЫМ entrypoint, поэтому семантика этих эндпоинтов
    одинакова везде и не может разойтись между приложениями.
    """

    @app.get("/health")
    def health_check():
        return build_health_payload()

    @app.get("/ready")
    def readiness_check():
        status_code, payload = build_readiness_payload()
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/safety")
    def safety_summary():
        return build_safety_summary()

    @app.get("/monitor/status")
    def monitor_status():
        from api.paper_monitor import paper_monitor

        return paper_monitor.status()

    return app
