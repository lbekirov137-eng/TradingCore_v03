class Retest:

    @staticmethod
    def detect(context, opening_range, breakout):

        if not breakout["confirmed"]:
            return {
                "confirmed": False,
            }

        market = context.market

        closes = market.closes
        timestamps = market.timestamps

        last_price = closes[-1]

        tolerance = opening_range["range"] * 0.15

        if breakout["direction"] == "LONG":

            if abs(last_price - opening_range["high"]) <= tolerance:

                return {
                    "confirmed": True,
                    "price": last_price,
                    "timestamp": timestamps[-1],
                }

        if breakout["direction"] == "SHORT":

            if abs(last_price - opening_range["low"]) <= tolerance:

                return {
                    "confirmed": True,
                    "price": last_price,
                    "timestamp": timestamps[-1],
                }

        return {
            "confirmed": False,
        }