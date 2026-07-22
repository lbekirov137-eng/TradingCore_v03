from api.contracts.context import MarketContext
from api.decision_engine.rule_registry import RuleRegistry


class DecisionEngine:

    NAME = "Decision Engine"
    VERSION = "3.0.0"

    @staticmethod
    def process(context: MarketContext) -> MarketContext:

        rules = {}

        critical_failed = False
        total_score = 0
        total_confidence = 0.0
        passed_rules = 0
        failed_rules = []

        for rule in RuleRegistry.get_rules():

            result = rule.evaluate(context)

            rule_name = rule.NAME

            rules[rule_name] = result

            if result["passed"]:
                total_score += result["score"]
                total_confidence += result["confidence"]
                passed_rules += 1
            else:
                failed_rules.append(rule_name)

            if result["critical"] and not result["passed"]:
                critical_failed = True

        context.rules = rules

        confidence = (
            round(total_confidence / passed_rules, 2)
            if passed_rules > 0
            else 0.0
        )

        if critical_failed:
            decision = "NO_TRADE"
            reason = "Critical rule failed"
        else:
            decision = "TRADE" if total_score >= 30 else "NO_TRADE"
            reason = "Score evaluation"

        context.decision = {
            "decision": decision,
            "score": total_score,
            "confidence": confidence,
            "failed_rules": failed_rules,
            "reason": reason,
        }

        return context