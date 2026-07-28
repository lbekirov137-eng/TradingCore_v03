class TakeProfit:

    @staticmethod
    def calculate(entry, stop, direction="LONG"):
        """
        CRITICAL FIX (found via real 6-month backtest, not by inspection):
        this used to always compute entry + risk*N regardless of direction.
        For a SHORT trade that places the take-profit ABOVE both entry and
        the stop -- on the wrong side of the market entirely, unreachable
        in the profitable (downward) direction. In backtest this produced
        a SHORT position that opened, price fell ~33% in its favor, and it
        never closed because neither the (correct) stop nor the (bogus,
        upward) take-profit was ever touched -- silently blocking all
        further trading for the rest of the dataset. See
        tests/regression/test_take_profit_direction.py.
        """

        risk = abs(entry - stop)

        if direction == "SHORT":
            return {
                "tp1": entry - risk * 2,
                "tp2": entry - risk * 3,
                "risk_reward": "1:2 / 1:3",
            }

        return {
            "tp1": entry + risk * 2,
            "tp2": entry + risk * 3,
            "risk_reward": "1:2 / 1:3",
        }
