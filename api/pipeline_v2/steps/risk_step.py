from api.pipeline_v2.steps.base_step import BaseStep
from api.contracts.context import MarketContext

from api.risk_engine import RiskEngine


class RiskStep(BaseStep):

    NAME = "Risk Step"
    VERSION = "2.0.0"

    def process(self, context: MarketContext) -> MarketContext:

        price = context.market["price"]
        atr = context.indicators["atr"]["value"]

        risk = RiskEngine.calculate(
            balance=1000,
            risk_percent=0.1,
            price=price,
            atr=atr,
        )

        context.risk = risk

        context.audit["risk_step"] = {
            "status": "OK"
        }

        return context