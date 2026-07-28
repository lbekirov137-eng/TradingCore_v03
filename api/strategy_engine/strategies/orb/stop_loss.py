class StopLoss:

    @staticmethod
    def calculate(context, opening_range, breakout):

        atr = context.indicators["atr"]["value"]

        if breakout["direction"] == "LONG":

            return {
                "stop": opening_range["low"] - atr * 0.2
            }

        return {
            "stop": opening_range["high"] + atr * 0.2
        }