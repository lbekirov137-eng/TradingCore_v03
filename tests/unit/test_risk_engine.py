import math

from api.risk_engine import RiskEngine, DailyRiskGuard


class TestRiskEngineIndependentRecalculation:
    """
    Каждая проверка независимо пересчитывает ожидаемый результат по
    формуле risk_amount = balance * risk_percent/100,
    position_size = risk_amount / stop_distance,
    а не просто сверяется с внутренней реализацией.
    """

    def test_normal_case_matches_hand_calculation(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=2.0)

        expected_risk_amount = 1000.0 * (0.1 / 100)  # 1.0
        expected_position_size = expected_risk_amount / 2.0  # 0.5

        assert result["allowed"] is True
        assert result["risk_amount"] == round(expected_risk_amount, 2)
        assert result["position_size"] == round(expected_position_size, 6)

    def test_risk_amount_is_approximately_point_one_percent(self):
        result = RiskEngine.calculate(balance=10_000.0, risk_percent=0.1, price=50.0, atr=5.0)
        assert result["risk_amount"] == 10.0  # 0.1% of 10,000


class TestRiskEngineBoundaryCases:
    """
    CRITICAL finding: до фикса `atr <= 0` пропускал NaN и inf, так как
    `float('nan') <= 0` в Python равно False. Это давало "allowed: True"
    с NaN/inf размером позиции — фиктивное одобрение сделки при
    отсутствующих/повреждённых данных.
    """

    def test_nan_atr_is_rejected(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=float("nan"))
        assert result["allowed"] is False

    def test_infinite_atr_is_rejected(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=float("inf"))
        assert result["allowed"] is False

    def test_zero_atr_is_rejected(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=0.0)
        assert result["allowed"] is False

    def test_negative_atr_is_rejected(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=-5.0)
        assert result["allowed"] is False

    def test_zero_balance_is_rejected(self):
        result = RiskEngine.calculate(balance=0.0, risk_percent=0.1, price=100.0, atr=2.0)
        assert result["allowed"] is False

    def test_negative_balance_is_rejected(self):
        result = RiskEngine.calculate(balance=-1000.0, risk_percent=0.1, price=100.0, atr=2.0)
        assert result["allowed"] is False

    def test_negative_risk_percent_is_rejected(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=-0.1, price=100.0, atr=2.0)
        assert result["allowed"] is False

    def test_zero_price_is_rejected(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=0.0, atr=2.0)
        assert result["allowed"] is False

    def test_nan_balance_is_rejected(self):
        result = RiskEngine.calculate(balance=float("nan"), risk_percent=0.1, price=100.0, atr=2.0)
        assert result["allowed"] is False

    def test_none_atr_is_rejected_not_crashed(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=None)
        assert result["allowed"] is False


class TestDailyRiskGuard:

    def test_allows_first_trade(self):
        check = DailyRiskGuard.check(balance=1000.0, risk_amount=1.0, max_trades=3, max_risk_percent=1.0)
        assert check["allowed"] is True

    def test_blocks_after_max_trades(self):
        for _ in range(3):
            DailyRiskGuard.register_trade(risk_amount=1.0)

        check = DailyRiskGuard.check(balance=1000.0, risk_amount=1.0, max_trades=3, max_risk_percent=100.0)
        assert check["allowed"] is False
        assert "лимит сделок" in check["reason"]

    def test_blocks_when_daily_risk_budget_exceeded(self):
        # max daily risk = 1000 * 1% = 10.0
        DailyRiskGuard.register_trade(risk_amount=9.5)

        check = DailyRiskGuard.check(balance=1000.0, risk_amount=1.0, max_trades=100, max_risk_percent=1.0)
        assert check["allowed"] is False
        assert "лимит риска" in check["reason"]

    def test_allows_within_daily_risk_budget(self):
        DailyRiskGuard.register_trade(risk_amount=5.0)

        check = DailyRiskGuard.check(balance=1000.0, risk_amount=4.0, max_trades=100, max_risk_percent=1.0)
        assert check["allowed"] is True
