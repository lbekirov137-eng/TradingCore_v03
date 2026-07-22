from api.decision_engine.rules.base_rule import BaseRule


class TrendRule(BaseRule):

    NAME = "Trend Rule"
    VERSION = "3.0.0"

    @staticmethod
    def evaluate(context):
        selected_trade = (
            context.strategy.get("selected_trade", {})
            if isinstance(context.strategy, dict)
            else {}
        )
        signal = selected_trade.get("signal")

        ema = context.indicators.get("ema", {})
        trend = ema.get("trend")

        if signal == "NO TRADE":
            return {
                "passed": False,
                "critical": False,
                "score": 0,
                "confidence": 0.0,
                "direction": None,
                "reason": "No selected trade",
            }

        expected_trend = (
            "BULLISH" if signal == "BUY"
            else "BEARISH" if signal == "SELL"
            else None
        )

        if trend == expected_trend:
            return {
                "passed": True,
                "critical": False,
                "score": 20,
                "confidence": 0.90,
                "direction": "LONG" if signal == "BUY" else "SHORT",
                "reason": "EMA trend confirms selected trade direction",
            }

        return {
            "passed": False,
            "critical": False,
            "score": 0,
            "confidence": 0.0,
            "direction": "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else None,
            "reason": "EMA trend does not confirm selected trade direction",
        }
