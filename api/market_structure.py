class MarketStructure:

    @staticmethod
    def analyze(highs, lows):

        if len(highs) < 2 or len(lows) < 2:
            return {
                "structure": "UNKNOWN"
            }

        last_high = highs[-1]
        prev_high = highs[-2]

        last_low = lows[-1]
        prev_low = lows[-2]

        if last_high > prev_high and last_low > prev_low:
            structure = "UPTREND"

        elif last_high < prev_high and last_low < prev_low:
            structure = "DOWNTREND"

        else:
            structure = "RANGE"

        return {
            "structure": structure,
            "last_high": round(last_high, 2),
            "last_low": round(last_low, 2),
        }