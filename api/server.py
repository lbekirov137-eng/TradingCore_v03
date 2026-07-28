import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config.startup_safety import assert_safe_startup, StartupSafetyError

# ---------------------------------------------------------------------
# STARTUP SAFETY GATE — must run before anything else in this module.
# Небезопасная или нераспознанная конфигурация -> процесс не запускается.
# Значения секретов здесь никогда не читаются и не печатаются.
# ---------------------------------------------------------------------
try:
    _STARTUP_SUMMARY = assert_safe_startup()
    print(f"[STARTUP SAFETY] OK: {_STARTUP_SUMMARY}", file=sys.stderr, flush=True)
except StartupSafetyError as _startup_error:
    print(f"[STARTUP SAFETY] REFUSED TO START: {_startup_error}", file=sys.stderr, flush=True)
    raise

from api.analyzer import MarketAnalyzer
from api.contracts.context import LiveContext
from api.workflow.workflow import Workflow
from api.decision_engine.decision_engine import kill_switch
from config.settings import (
    DEFAULT_SYMBOL,
    DEFAULT_INTERVAL,
    DEFAULT_CANDLE_LIMIT,
    PAPER_TRADING,
    LIVE_TRADING,
)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """
    Запускает фоновый cloud paper monitor ТОЛЬКО если явно включён через
    ENABLE_CLOUD_MONITOR=true. По умолчанию выключен — это важно для
    тестов (TestClient не должен незаметно запускать поток, стучащийся
    в реальные биржевые эндпоинты на каждый прогон pytest) и для
    локального использования только через ручной GET /paper/tick.

    Для облачного (Railway) paper-forward запуска ENABLE_CLOUD_MONITOR=true
    обязателен — иначе процесс просто отвечает на HTTP и не ведёт
    никакого автономного наблюдения.
    """
    if os.getenv("ENABLE_CLOUD_MONITOR", "").strip().lower() in ("1", "true", "yes", "on"):
        from api.cloud_monitor import monitor
        monitor.start()

    yield

    from api.cloud_monitor import monitor
    monitor.stop()


app = FastAPI(
    title="TradingCore API",
    version="0.1",
    lifespan=_lifespan,
)


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "TradingCore API"
    }


@app.get("/analyze")
def analyze():
    return MarketAnalyzer.analyze()


@app.get("/paper/tick")
def paper_tick(
    exchange: str = "binance",
    symbol: str = DEFAULT_SYMBOL,
    interval: str = DEFAULT_INTERVAL,
    limit: int = DEFAULT_CANDLE_LIMIT,
    replay: bool = False,
):
    """
    Один тик paper/demo-контура: рыночные данные -> индикаторы ->
    фильтры режима -> стратегия -> Decision Engine -> paper-исполнение.

    Реальные ордера не отправляются ни при каких условиях — здесь нет
    кода, вызывающего живой ордер на бирже.

    replay=true отключает проверку устаревания данных по стенным часам
    (используется для детерминированного воспроизведения исторических
    данных). В обычном paper-forward запуске должен оставаться false,
    иначе система перестанет замечать залипший фид.
    """

    context = LiveContext(
        exchange=exchange,
        symbol=symbol,
        interval=interval,
        limit=limit,
        replay_mode=replay,
    )

    return Workflow.run(context)


@app.get("/safety")
def safety_summary():
    """Сводка безопасности запуска. Значения секретов никогда не выводятся."""

    from config.settings import DEMO_ONLY

    return {
        "paper_trading": PAPER_TRADING,
        "live_trading": LIVE_TRADING,
        "demo_only": DEMO_ONLY,
        "live_order_code_present": False,
        "kill_switch_engaged": kill_switch.is_engaged(),
    }


@app.get("/health")
def health_check():
    """
    Health endpoint для облачного/оркестрационного мониторинга (Railway и т.п.).

    Никогда не включает значения секретов — только флаги режима,
    временные метки и агрегированные счётчики.
    """
    from api.observability.states import health
    from api.position_manager.position_manager import PositionManager
    from api.cloud_monitor import monitor
    from config.settings import MAX_DATA_AGE_SECONDS, DEMO_ONLY
    import time

    health_status = health.status()

    data_age = health_status.get("market_data_age_seconds")

    if data_age is None:
        feed_state = "UNKNOWN"
    elif data_age > MAX_DATA_AGE_SECONDS:
        feed_state = "STALE"
    else:
        feed_state = "OK"

    seconds_since_heartbeat = health_status.get("seconds_since_heartbeat")

    if seconds_since_heartbeat is None:
        app_status = "STARTING"
    elif seconds_since_heartbeat > MAX_DATA_AGE_SECONDS:
        app_status = "DEGRADED"
    else:
        app_status = "HEALTHY"

    return {
        "status": app_status,
        "mode": "PAPER",
        "paper_trading": PAPER_TRADING,
        "live_trading": LIVE_TRADING,
        "demo_only": DEMO_ONLY,
        "monitor_running": monitor.is_running(),
        "last_candle_timestamp_ms": health_status.get("last_market_data_timestamp"),
        "data_feed_state": feed_state,
        "open_virtual_positions": 1 if PositionManager.has_open_position() else 0,
        "last_cycle_timestamp": health_status.get("last_heartbeat"),
        "uptime_seconds": health_status.get("uptime_seconds"),
        "server_time": time.time(),
    }


@app.get("/ready")
def readiness_check():
    """
    Готовность к работе (Railway readiness probe). В отличие от /health
    (который просто отражает состояние), /ready активно ПЕРЕПРОВЕРЯЕТ
    конфигурацию и возвращает FAILED_SAFELY (HTTP 503) при обнаружении:
      - попытки live-режима или нераспознанной конфигурации в рантайме;
      - устаревших рыночных данных;
      - повреждённого состояния (kill switch не смог прочитать свой файл
        состояния и поэтому находится в fail-closed режиме).

    Секреты никогда не включаются в ответ.
    """
    from fastapi.responses import JSONResponse
    from config.startup_safety import runtime_safety_check
    from config.settings import MAX_DATA_AGE_SECONDS
    from api.observability.states import health

    reasons = []

    runtime_safety = runtime_safety_check()
    if not runtime_safety["safe"]:
        reasons.append(f"unsafe_configuration: {runtime_safety['reason']}")

    health_status = health.status()
    data_age = health_status.get("market_data_age_seconds")
    if data_age is not None and data_age > MAX_DATA_AGE_SECONDS:
        reasons.append(f"stale_market_data: {data_age:.0f}s old (limit {MAX_DATA_AGE_SECONDS}s)")

    kill_switch_state = kill_switch.status()
    if "повреждено" in (kill_switch_state.get("reason") or ""):
        reasons.append("corrupted_state: kill switch state file could not be read (fail-closed)")

    ready = len(reasons) == 0

    body = {
        "ready": ready,
        "status": "READY" if ready else "FAILED_SAFELY",
        "reasons": reasons,
    }

    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/kill-switch/status")
def kill_switch_status():
    return kill_switch.status()


@app.post("/kill-switch/engage")
def kill_switch_engage(reason: str, operator: str = "operator", close_positions: bool = False):
    """
    Останавливает открытие новых сделок немедленно. Мониторинг уже
    открытых позиций продолжается. Позиции закрываются автоматически
    ТОЛЬКО если close_positions=true передан явно.
    """
    return kill_switch.engage(reason=reason, operator=operator, close_positions=close_positions)


@app.post("/kill-switch/disengage")
def kill_switch_disengage(operator: str = "operator"):
    """Процедура восстановления: явно снимает kill switch и разрешает новые сделки."""
    return kill_switch.disengage(operator=operator)


# ---------------------------------------------------------------------
# Observability (Phase 8) — никогда не выводит значения секретов
# ---------------------------------------------------------------------

@app.get("/observability/health")
def observability_health():
    from api.observability.states import health
    return health.status()


@app.get("/observability/pnl")
def observability_pnl():
    from api.observability.reports import pnl_report
    return pnl_report()


@app.get("/observability/position")
def observability_position():
    from api.observability.reports import open_position_report
    return open_position_report()


@app.get("/observability/risk")
def observability_risk():
    from api.observability.reports import risk_report
    return risk_report()


@app.get("/observability/logs")
def observability_logs(limit: int = 50):
    from api.observability.states import logger
    return {"logs": logger.recent(limit)}


@app.get("/observability/notifications")
def observability_notifications(limit: int = 50):
    from api.observability.telegram_mock import telegram
    return {
        "configured": telegram.is_configured(),
        "transport": "mock",
        "messages": telegram.recent(limit),
    }


@app.get("/observability/support-bundle")
def observability_support_bundle():
    """Диагностический пакет БЕЗ секретов (только флаги наличия переменных)."""
    from api.observability.states import build_support_bundle
    return build_support_bundle()


@app.get("/observability/clock-skew")
def observability_clock_skew(exchange: str = "binance"):
    """Сравнивает время сервера биржи с локальными часами хоста."""
    from api.binance import BinanceAPI
    from api.bybit import BybitAPI
    from api.market_data.resilience import ClockSkewChecker

    try:
        server_time = BinanceAPI.get_server_time() if exchange == "binance" else BybitAPI.get_server_time()
        return {"exchange": exchange, **ClockSkewChecker.check(server_time)}
    except Exception as error:
        return {"exchange": exchange, "error": f"{type(error).__name__}: {error}"}


@app.get("/paper-forward/journal")
def paper_forward_journal_export(limit: int = 500):
    """Экспорт paper-forward журнала (JSON). Секретов в записях нет."""
    from api.observability.paper_forward_journal import journal
    entries = journal.read_all()
    return {"count": len(entries), "entries": entries[-limit:]}


@app.get("/strategies/status")
def strategies_status():
    """
    Явный статус исследовательских стратегий. Обе НЕ одобрены для
    продакшн/demo-торговли — только для paper-forward наблюдения.
    См. AUTOTRADING_BACKTEST_REPORT.md.
    """
    from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy
    from api.strategy_engine.strategies.vwap.vwap_strategy import VWAPTrendPullbackStrategy

    return {
        strategy.NAME: {
            "version": strategy.VERSION,
            "status": strategy.STATUS,
            "production_approved": strategy.PRODUCTION_APPROVED,
        }
        for strategy in (ORBStrategy, VWAPTrendPullbackStrategy)
    }


@app.get("/demo/preflight")
def demo_preflight():
    """
    Проверка конфигурации Bybit Demo без подключения и без вывода секретов.
    Возвращает ready=false, пока пользователь не введёт свои demo-ключи.
    """
    from api.exchanges.bybit_demo.config import validate_demo_configuration, ConfigurationError

    try:
        return validate_demo_configuration()
    except ConfigurationError as error:
        return {"ready": False, "reason": str(error)}