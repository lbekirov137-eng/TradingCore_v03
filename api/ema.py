import pandas as pd


class EMAEngine:

    @staticmethod
    def calculate_all(closes):

        series = pd.Series(closes)

        ema20 = series.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = series.ewm(span=50, adjust=False).mean().iloc[-1]
        ema200 = series.ewm(span=200, adjust=False).mean().iloc[-1]

        price = closes[-1]

        if price > ema20 > ema50 > ema200:
            trend = "BULLISH"

        elif price < ema20 < ema50 < ema200:
            trend = "BEARISH"

        else:
            trend = "RANGE"

        return {
            "ema20": round(float(ema20), 2),
            "ema50": round(float(ema50), 2),
            "ema200": round(float(ema200), 2),
            "trend": trend,
        }