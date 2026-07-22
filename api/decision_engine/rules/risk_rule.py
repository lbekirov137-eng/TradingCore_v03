from api.contracts.context import MarketContext
from api.decision_engine.rules.base_rule import BaseRule


class RiskRule(BaseRule):

    NAME = "Risk Rule"
    VERSION = "2.0.0"

    def evaluate(
        self,
        context: MarketContext,
    ) -> dict:
        risk = context.risk

        if not isinstance(risk, dict):
            return {
                "passed": False,
                "critical": True,
                "score": 0,
                "confidence": 1.0,
                "direction": None,
                "reason": (
                    "Данные риска отсутствуют "
                    "или имеют неверный формат."
                ),
            }

        if risk.get("allowed") is True:
            return {
                "passed": True,
                "critical": False,
                "score": 30,
                "confidence": 1.0,
                "direction": None,
                "reason": "Риск соответствует настройкам.",
            }

        risk_reason = risk.get("reason")

        if risk_reason == "Strategy returned NO TRADE":
            reason = (
                "Расчёт риска пропущен: "
                "стратегия вернула NO TRADE."
            )
        else:
            reason = "Риск-лимит превышен."

        return {
            "passed": False,
            "critical": True,
            "score": 0,
            "confidence": 1.0,
            "direction": None,
            "reason": reason,
        }