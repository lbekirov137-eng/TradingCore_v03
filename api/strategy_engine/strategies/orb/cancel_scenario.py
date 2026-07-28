class CancelScenario:

    @staticmethod
    def check(
        context,
        opening_range,
        breakout,
        retest,
        confirmation,
    ):

        # Если подтверждения нет — сценарий отменяется
        if not confirmation["confirmed"]:
            return {
                "cancel": True,
                "reason": confirmation["reason"],
            }

        # Пока дополнительных причин отмены нет.
        # Далее здесь будут появляться:
        # - EMA Filter
        # - Volume Filter
        # - Wyckoff Filter
        # - Funding Filter
        # - Open Interest Filter
        # - Liquidity Filter
        # - News Filter
        # - Volatility Filter

        return {
            "cancel": False,
            "reason": None,
        }