import pandas as pd


class RSIEngine:

    @staticmethod
    def calculate(closes, period=14):

        series = pd.Series(closes)

        delta = series.diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()

        rs = avg_gain / avg_loss

        rsi = 100 - (100 / (1 + rs))

        value = float(rsi.iloc[-1])

        return {
            "value": round(value, 2)
        }