class SignalEngine:

    @staticmethod
    def generate(
        trend: str,
        structure: dict,
        rsi: dict,
    ):

        market_structure = structure["structure"]
        rsi_value = rsi["value"]

        # BUY
        if (
            trend == "BULLISH"
            and market_structure == "UPTREND"
            and rsi_value < 70
        ):
            return {
                "signal": "BUY"
            }

        # SELL
        if (
            trend == "BEARISH"
            and market_structure == "DOWNTREND"
            and rsi_value > 30
        ):
            return {
                "signal": "SELL"
            }

        return {
            "signal": "NO TRADE"
        }