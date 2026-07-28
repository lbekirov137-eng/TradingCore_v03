import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config.startup_safety import assert_safe_startup, StartupSafetyError

# ---------------------------------------------------------------------
# STARTUP SAFETY GATE — выполняется при импорте модуля, ДО создания
# приложения и до того, как uvicorn начнёт слушать порт.
#
# Небезопасная или нераспознанная конфигурация -> процесс не стартует:
#   - LIVE_TRADING=true
#   - TRADING_ENVIRONMENT=LIVE
#   - TRADING_ENVIRONMENT с любым нераспознанным значением (fail-closed)
#
# Значения секретов здесь не читаются и не печатаются — только флаги режима.
# ---------------------------------------------------------------------
try:
    _STARTUP_SUMMARY = assert_safe_startup()
    print(f"[STARTUP SAFETY] OK: {_STARTUP_SUMMARY}", file=sys.stderr, flush=True)
except StartupSafetyError as _startup_error:
    print(f"[STARTUP SAFETY] REFUSED TO START: {_startup_error}", file=sys.stderr, flush=True)
    raise

from api.analyzer import MarketAnalyzer
from api.paper_monitor import paper_monitor


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Управляет фоновым PAPER-монитором вместе с жизненным циклом сервера.

    Монитор выключен по умолчанию (PAPER_MONITOR_ENABLED); при выключенном
    флаге start() лишь фиксирует статус DISABLED и ничего не запускает.

    Отказ монитора намеренно НЕ роняет сервер: healthcheck Railway смотрит
    на /health, и потеря наблюдательного цикла не должна приводить к
    рестарт-петле всего сервиса. Реальное состояние видно в /monitor/status.
    """
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


app = FastAPI(
    title="TradingCore API",
    version="0.1",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "TradingCore API",
        "mode": "PAPER",
    }


@app.get("/analyze")
def analyze():
    return MarketAnalyzer.analyze()


@app.get("/safety")
def safety_summary():
    """Сводка безопасности. Значения секретов никогда не выводятся."""
    from config.startup_safety import (
        get_live_trading_flag,
        get_paper_trading_flag,
        get_demo_only_flag,
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


@app.get("/health")
def health_check():
    """
    Health endpoint для Railway/оркестрации.

    Отражает состояние процесса. Никогда не содержит секретов.
    """
    from config.startup_safety import (
        get_live_trading_flag,
        get_paper_trading_flag,
        get_demo_only_flag,
    )

    return {
        "status": "HEALTHY",
        "mode": "PAPER",
        "paper_trading": get_paper_trading_flag(),
        "live_trading": get_live_trading_flag(),
        "demo_only": get_demo_only_flag(),
        "server_time": time.time(),
    }


@app.get("/ready")
def readiness_check():
    """
    Readiness probe. В отличие от /health, активно ПЕРЕПРОВЕРЯЕТ
    конфигурацию в рантайме и возвращает FAILED_SAFELY (HTTP 503),
    если обнаружены:
      - попытка live-режима или нераспознанная конфигурация;
      - плечо выше разрешённого для PAPER-запуска (1x);
      - риск на сделку выше 0.1%.

    Правило: неизвестная конфигурация, попытка live-режима или
    повреждённое состояние => остановка как FAILED_SAFELY, а не
    молчаливое продолжение работы.
    """
    from config.startup_safety import runtime_safety_check
    from api.leverage_risk_engine import LeverageRiskEngine

    reasons = []

    runtime_safety = runtime_safety_check()
    if not runtime_safety["safe"]:
        reasons.append(f"unsafe_configuration: {runtime_safety['reason']}")

    if LeverageRiskEngine.MAX_LEVERAGE > 1:
        reasons.append(
            f"leverage_above_paper_limit: MAX_LEVERAGE={LeverageRiskEngine.MAX_LEVERAGE} (must be 1)"
        )

    if LeverageRiskEngine.MAX_RISK_PERCENT > 0.001:
        reasons.append(
            f"risk_above_limit: MAX_RISK_PERCENT={LeverageRiskEngine.MAX_RISK_PERCENT} (must be <= 0.001)"
        )

    ready = len(reasons) == 0

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "ready": ready,
            "status": "READY" if ready else "FAILED_SAFELY",
            "reasons": reasons,
        },
    )


@app.get("/monitor/status")
def monitor_status():
    """
    Состояние фонового PAPER-монитора.

    Отвечает на вопрос «идёт ли наблюдательный цикл в облаке»: включён ли
    он, работает ли поток, сколько было перезапусков и какая последняя
    ошибка. Секретов не содержит; real_orders_enabled всегда False.
    """
    return paper_monitor.status()


@app.get("/strategies/status")
def strategies_status():
    """
    Статус исследовательских стратегий.

    ORB и VWAP признаны неприбыльными в текущем виде и помечены как
    RESEARCH_ONLY — они используются только для сбора честной
    paper-статистики, не для продакшн-торговли.
    """
    return {
        "VLAD_ORB": {
            "status": "RESEARCH_ONLY",
            "production_approved": False,
            "note": "Активная стратегия paper-контура координатора.",
        },
        "ORB_LEGACY": {
            "status": "RESEARCH_ONLY",
            "production_approved": False,
            "note": (
                "Наследуемая реализация в api/strategy_engine/strategies/orb/. "
                "Backtest: 208 сделок за 6 месяцев, net -176.54 (-17.7%), "
                "profit factor 0.205, walk-forward consistency 0.0%."
            ),
        },
        "VWAP_TREND_PULLBACK": {
            "status": "RESEARCH_ONLY",
            "production_approved": False,
            "note": (
                "Backtest: 98 сделок, net -128.37 (-12.8%), "
                "profit factor 0.092, walk-forward consistency 0.0%."
            ),
        },
    }
