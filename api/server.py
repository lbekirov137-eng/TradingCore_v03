from fastapi import FastAPI

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

app = FastAPI(
    title="TradingCore API",
    version="0.1"
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

    return {
        "paper_trading": PAPER_TRADING,
        "live_trading": LIVE_TRADING,
        "live_order_code_present": False,
        "kill_switch_engaged": kill_switch.is_engaged(),
    }


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