"""
Второй допустимый entrypoint TradingCore.

РАНЬШЕ ЭТОТ ФАЙЛ БЫЛ ДЫРОЙ В ЗАЩИТЕ. Он создавал собственное
FastAPI-приложение вообще без startup safety gate: `uvicorn api.main:app`
поднимался при LIVE_TRADING=true, не имел /ready и /safety, а /health
безусловно отвечал {"status": "healthy"} независимо от конфигурации.
Достаточно было указать другой модуль в start command, чтобы обойти всю
защиту облачного entrypoint.

Теперь оба приложения используют ОДИН модуль api/app_safety:
  - тот же startup safety gate (отказ запуска при небезопасном режиме);
  - тот же lifespan фонового PAPER-монитора (выключен по умолчанию,
    корректно останавливается при shutdown);
  - те же /health, /ready, /safety, /monitor/status.

Вторая независимая реализация не создаётся: изменение защиты в
app_safety автоматически действует на оба entrypoint.
"""

from fastapi import FastAPI

from api.app_safety import (
    enforce_startup_safety,
    paper_monitor_lifespan,
    register_safety_routes,
)

# Гейт выполняется ДО создания приложения — как и в api/server.py.
_STARTUP_SUMMARY = enforce_startup_safety()

from api.analyzer import MarketAnalyzer

app = FastAPI(
    title="TradingCore",
    version="0.3.0",
    lifespan=paper_monitor_lifespan,
)

# /health, /ready, /safety, /monitor/status — общая реализация.
register_safety_routes(app)


@app.get("/")
def root():
    return {
        "status": "OK",
        "service": "TradingCore",
        "version": "0.3.0",
        "mode": "PAPER",
    }


@app.get("/ping")
def ping():
    return {
        "ping": "pong",
    }


@app.get("/market")
def market():
    return MarketAnalyzer.analyze()
