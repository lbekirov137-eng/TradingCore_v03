class Breakout:

    @staticmethod
    def detect(context, opening_range):

        market = context.visible_market

        closes = market.closes
        highs = market.highs
        lows = market.lows

        if len(closes) < 2:
            return {
                "confirmed": False,
                "direction": None,
                "strength": 0.0,
            }

        previous_close = closes[-2]
        last_close = closes[-1]

        # LONG
        if (
            previous_close <= opening_range["high"]
            and last_close > opening_range["high"]
        ):

            strength = (
                last_close - opening_range["high"]
            ) / opening_range["range"]

            return {
                "confirmed": True,
                "direction": "LONG",
                "strength": round(strength, 3),
            }

        # SHORT
        if (
            previous_close >= opening_range["low"]
            and last_close < opening_range["low"]
        ):

            strength = (
                opening_range["low"] - last_close
            ) / opening_range["range"]

            return {
                "confirmed": True,
                "direction": "SHORT",
                "strength": round(strength, 3),
            }

        return {
            "confirmed": False,
            "direction": None,
            "strength": 0.0,
        }