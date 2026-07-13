class RiskEngine:

    @staticmethod
    def calculate(
        balance: float,
        risk_percent: float,
        price: float,
        atr: float,
    ):

        if atr <= 0:
            return {
                "allowed": False,
                "reason": "ATR is zero",
            }

        risk_amount = balance * (risk_percent / 100)

        stop_distance = atr

        position_size = risk_amount / stop_distance

        return {
            "allowed": True,
            "risk_amount": round(risk_amount, 2),
            "position_size": round(position_size, 6),
            "stop_distance": round(stop_distance, 2),
        }