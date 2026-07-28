class RiskRule:

    @staticmethod
    def evaluate(context):

        risk = context.risk

        if not risk.get("allowed", False):
            return {
                "passed": False,
                "reason": "Риск-лимит превышен.",
            }

        return {
            "passed": True,
            "reason": "Риск соответствует настройкам.",
        }