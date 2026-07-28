class Confirmation:

    @staticmethod
    def check(context, breakout, retest):

        if not breakout["confirmed"]:
            return {
                "confirmed": False,
                "reason": "Нет подтвержденного пробоя.",
            }

        if not retest["confirmed"]:
            return {
                "confirmed": False,
                "reason": "Нет подтвержденного ретеста.",
            }

        return {
            "confirmed": True,
            "reason": "Пробой и ретест подтверждены.",
        }