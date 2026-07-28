from api.pipeline_v2.steps.base_step import BaseStep
from api.contracts.context import MarketContext

from api.signal_engine import SignalEngine


class StrategyStep(BaseStep):

    NAME = "Strategy Step"
    VERSION = "2.0.0"

    def process(self, context: MarketContext) -> MarketContext:

        ema = context.indicators["ema"]
        rsi = context.indicators["rsi"]
        structure = context.indicators["structure"]

        signal = SignalEngine.generate(
            trend=ema["trend"],
            structure=structure,
            rsi=rsi,
        )

        context.strategy = signal

        context.audit["strategy_step"] = {
            "status": "OK"
        }

        return context