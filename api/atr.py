import pandas as pd


class ATREngine:

    @staticmethod
    def calculate(highs, lows, closes, period=14):

        high = pd.Series(highs)
        low = pd.Series(lows)
        close = pd.Series(closes)

        tr = pd.concat(
            [
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = tr.rolling(period).mean()

        value = float(atr.iloc[-1])

        return {
            "value": round(value, 2)
        }