class Entry:

    @staticmethod
    def calculate(context, opening_range, breakout, confirmation):

        if not confirmation["confirmed"]:
            return None

        market = context.market

        closes = market.closes
        timestamps = market.timestamps

        entry_price = closes[-1]

        return {
            "entry": entry_price,
            "direction": breakout["direction"],
            "timestamp": timestamps[-1],
        }