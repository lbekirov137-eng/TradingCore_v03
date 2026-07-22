from api.core.engine import CoreEngine

from api.pipeline_v2.steps.indicator_step import IndicatorStep
from api.market_intelligence_step import MarketIntelligenceStep
from api.pipeline_v2.steps.strategy_step import StrategyStep
from api.pipeline_v2.steps.vlad_orb_observer_step import (
    VladORBObserverStep,
)
from api.pipeline_v2.steps.strategy_coordinator_step import (
    StrategyCoordinatorStep,
)
from api.pipeline_v2.steps.risk_step import RiskStep
from api.pipeline_v2.steps.trade_plan_step import TradePlanStep
from api.pipeline_v2.steps.decision_step import DecisionStep
from api.pipeline_v2.steps.paper_execution_step import (
    PaperExecutionStep,
)


class Bootstrap:
    @staticmethod
    def build() -> CoreEngine:
        engine = CoreEngine()

        engine.register(
            "indicator",
            IndicatorStep(),
        )

        engine.register(
            "market_intelligence",
            MarketIntelligenceStep(),
        )

        engine.register(
            "strategy",
            StrategyStep(),
        )

        engine.register(
            "vlad_orb_observer",
            VladORBObserverStep(),
        )

        engine.register(
            "strategy_coordinator",
            StrategyCoordinatorStep(),
        )

        engine.register(
            "risk",
            RiskStep(),
        )

        engine.register(
            "trade_plan",
            TradePlanStep(),
        )

        engine.register(
            "decision",
            DecisionStep(),
        )

        # Paper execution only.
        # This module cannot send real orders.
        engine.register(
            "paper_execution",
            PaperExecutionStep(),
        )

        return engine
