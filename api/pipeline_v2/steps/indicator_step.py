from api.pipeline_v2.steps.base_step import BaseStep
from api.contracts.context import MarketContext

from api.ema import EMAEngine
from api.rsi import RSIEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure


class IndicatorStep(BaseStep):

    NAME = "Indicator Step"
    VERSION = "2.0.0"

    def process(self, context: MarketContext) -> MarketContext:

        closes = context.market["closes"]
        highs = context.market["highs"]
        lows = context.market["lows"]

        context.indicators["ema"] = EMAEngine.calculate_all(closes)

        context.indicators["rsi"] = RSIEngine.calculate(closes)

        context.indicators["atr"] = ATREngine.calculate(
            highs,
            lows,
            closes,
        )

        context.indicators["structure"] = MarketStructure.analyze(
            highs,
            lows,
        )

        context.audit["indicator_step"] = {
            "status": "OK"
        }

        return context