"""
Исполняемые реализации зарегистрированных стратегий.

Каждая реализована по Strategy Implementation Contract
(docs/STRATEGY_IMPLEMENTATION_CONTRACTS.md) и следует единому интерфейсу
из contracts.py: только закрытые свечи, детерминированный вывод, LONG
only, неизменяемая конфигурация, без побочных эффектов и обращений к
бирже.

Реестр ниже связывает strategy_id из api/strategy_supervisor/registry.py
с исполняемым классом. Отсутствие записи означает «спецификация есть,
реализации нет» — и это честнее, чем подставить похожую.
"""

from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    Candle,
    CandleWindow,
    LookAheadError,
    StrategyConfig,
    StrategyContractError,
    StrategyDecision,
    atr,
    candles_from_arrays,
    ema,
    session_vwap,
    swing_points,
)
from api.strategy_engine.strategies.london_session_breakout_retest import (
    LondonSessionBreakoutRetest,
)
from api.strategy_engine.strategies.session_vwap_trend_pullback import (
    SessionVwapTrendPullback,
)
from api.strategy_engine.strategies.trend_pullback_ema_structure import (
    TrendPullbackEmaStructure,
)


IMPLEMENTATIONS = {
    SessionVwapTrendPullback.strategy_key: SessionVwapTrendPullback,
    LondonSessionBreakoutRetest.strategy_key: LondonSessionBreakoutRetest,
    TrendPullbackEmaStructure.strategy_key: TrendPullbackEmaStructure,
}


def get_implementation(strategy_id: str):
    """
    Класс реализации или None.

    None — осмысленный ответ: ORB_0930_RETEST и RANGE_NO_TRADE_POLICY не
    имеют реализации в этом пакете (первая живёт в strategies/orb/, вторая
    является политикой). Подставлять «похожую» реализацию нельзя.
    """
    return IMPLEMENTATIONS.get(strategy_id)


__all__ = [
    "BaseStrategy",
    "Candle",
    "CandleWindow",
    "LookAheadError",
    "StrategyConfig",
    "StrategyContractError",
    "StrategyDecision",
    "IMPLEMENTATIONS",
    "get_implementation",
    "atr",
    "ema",
    "session_vwap",
    "swing_points",
    "candles_from_arrays",
    "SessionVwapTrendPullback",
    "LondonSessionBreakoutRetest",
    "TrendPullbackEmaStructure",
]
