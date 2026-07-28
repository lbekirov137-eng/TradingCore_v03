from api.core.engine import CoreEngine

from api.pipeline_v2.steps.indicator_step import IndicatorStep
from api.pipeline_v2.steps.strategy_step import StrategyStep
from api.pipeline_v2.steps.risk_step import RiskStep


class Bootstrap:

    @staticmethod
    def build():

        engine = CoreEngine()

        engine.register(
            "indicator",
            IndicatorStep(),
        )

        engine.register(
            "strategy",
            StrategyStep(),
        )

        engine.register(
            "risk",
            RiskStep(),
        )

        return engine