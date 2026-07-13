class TradePlan:

    @staticmethod
    def build(
        signal: str,
        price: float,
        atr: float,
    ):

        if signal == "BUY":

            stop = round(price - atr, 2)
            tp1 = round(price + atr * 2, 2)
            tp2 = round(price + atr * 3, 2)

        elif signal == "SELL":

            stop = round(price + atr, 2)
            tp1 = round(price - atr * 2, 2)
            tp2 = round(price - atr * 3, 2)

        else:

            return {
                "entry": None,
                "stop": None,
                "take_profit_1": None,
                "take_profit_2": None,
                "risk_reward": "NO TRADE",
            }

        return {
            "entry": round(price, 2),
            "stop": stop,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "risk_reward": "1:2 / 1:3",
        }