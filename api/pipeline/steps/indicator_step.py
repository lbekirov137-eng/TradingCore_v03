from api.pipeline.steps.base_step import BaseStep
from api.contracts.context import MarketContext

from api.ema import EMAEngine


class IndicatorStep(BaseStep):

    NAME = "Indicator Step"
    VERSION = "1.1.0"

    def process(self, context: MarketContext) -> MarketContext:

        closes = context.market["closes"]

        ema = EMAEngine.calculate_all(closes)

        context.indicators["ema"] = ema

        context.audit["indicator_step"] = {
            "status": "OK",
            "ema": "calculated",
        }

        return context