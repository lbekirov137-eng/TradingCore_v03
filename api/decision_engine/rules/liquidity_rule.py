from api.contracts.context import MarketContext
from api.decision_engine.rules.base_rule import BaseRule


class LiquidityRule(BaseRule):

    NAME = "Liquidity Rule"
    VERSION = "1.0.0"

    def evaluate(self, context: MarketContext) -> dict:

        volume = context.market.get("volume", 0)

        minimum_volume = 1000

        if volume < minimum_volume:
            return {
                "passed": False,
                "critical": True,
                "score": 0,
                "confidence": 1.0,
                "direction": None,
                "reason": "Недостаточная ликвидность.",
            }

        return {
            "passed": True,
            "critical": False,
            "score": 20,
            "confidence": 0.90,
            "direction": None,
            "reason": "Ликвидность достаточная.",
        }