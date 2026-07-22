from api.decision_engine.rules.trend_rule import TrendRule
from api.decision_engine.rules.session_rule import SessionRule
from api.decision_engine.rules.risk_rule import RiskRule
from api.decision_engine.rules.liquidity_rule import LiquidityRule
from api.decision_engine.rules.news_rule import NewsRule


class RuleRegistry:

    @staticmethod
    def get_rules():

        return [
            TrendRule(),
            SessionRule(),
            RiskRule(),
            LiquidityRule(),
            NewsRule(),
        ]