import os

from fastapi import FastAPI

from api.app_safety import (
    enforce_startup_safety,
    paper_monitor_lifespan,
    register_safety_routes,
)

# ---------------------------------------------------------------------
# STARTUP SAFETY GATE — общий для всех entrypoint (api/app_safety.py).
# Выполняется при импорте модуля, ДО создания приложения и до того, как
# uvicorn начнёт слушать порт. Небезопасная или нераспознанная
# конфигурация -> процесс не стартует.
#
# Реализация НЕ дублируется: api/main.py вызывает ровно те же функции,
# поэтому обойти защиту сменой entrypoint невозможно.
# ---------------------------------------------------------------------
_STARTUP_SUMMARY = enforce_startup_safety()

from api.analyzer import MarketAnalyzer

app = FastAPI(
    title="TradingCore API",
    version="0.1",
    lifespan=paper_monitor_lifespan,
)

# Общие safety-эндпоинты: /health, /ready, /safety, /monitor/status.
# Регистрируются из api/app_safety, а не описываются здесь повторно.
register_safety_routes(app)


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
