class TakeProfit:

    @staticmethod
    def calculate(entry, stop):

        risk = abs(entry - stop)

        return {
            "tp1": entry + risk * 2,
            "tp2": entry + risk * 3,
            "risk_reward": "1:2 / 1:3",
        }