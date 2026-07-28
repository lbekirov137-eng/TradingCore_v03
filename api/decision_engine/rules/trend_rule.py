class TrendRule:

    @staticmethod
    def evaluate(context):

        ema = context.indicators.get("ema", {})

        trend = ema.get("trend")

        if trend == "BULLISH":
            return {
                "passed": True,
                "direction": "LONG",
                "reason": "Бычий тренд EMA",
            }

        if trend == "BEARISH":
            return {
                "passed": True,
                "direction": "SHORT",
                "reason": "Медвежий тренд EMA",
            }

        return {
            "passed": False,
            "direction": None,
            "reason": "Тренд не определён",
        }