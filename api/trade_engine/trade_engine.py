class TradeEngine:

    @staticmethod
    def simulate(signal):

        if signal is None:
            return None

        return {
            "status": "OPEN",
            "direction": signal["direction"],
            "entry": signal["entry"],
            "stop": signal["stop"],
            "tp1": signal["tp1"],
            "tp2": signal["tp2"],
            "opened": True,
        }