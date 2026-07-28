"""
Regression tests for a confirmed CRITICAL audit finding, ported onto the
coordinator base branch.

Root cause: `if atr <= 0` / `if stop_distance <= 0` do NOT reject NaN or
Infinity, because in IEEE-754 `float('nan') <= 0` evaluates to False.
Before the fix, a NaN ATR (which ATREngine legitimately produces when
there are fewer than 14 candles -- startup, a data gap, a short-history
symbol) flowed straight through to `"allowed": True` with a NaN position
size, i.e. an approved trade of undefined size.

Both RiskEngine.calculate and RiskEngine.calculate_by_stop are live on
this branch (called from api/pipeline_v2/steps/risk_step.py), so both are
covered here.
"""

import pytest

from api.risk_engine import RiskEngine


NAN = float("nan")
INF = float("inf")
NEG_INF = float("-inf")


class TestCalculateRejectsNonFiniteInputs:

    @pytest.mark.parametrize("bad", [NAN, INF, NEG_INF, None, "5", True])
    def test_bad_atr_is_rejected(self, bad):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=bad)
        assert result["allowed"] is False

    @pytest.mark.parametrize("bad", [NAN, INF, None, "1000"])
    def test_bad_balance_is_rejected(self, bad):
        result = RiskEngine.calculate(balance=bad, risk_percent=0.1, price=100.0, atr=2.0)
        assert result["allowed"] is False

    @pytest.mark.parametrize("bad", [NAN, INF, None])
    def test_bad_price_is_rejected(self, bad):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=bad, atr=2.0)
        assert result["allowed"] is False

    @pytest.mark.parametrize("bad", [NAN, INF, None])
    def test_bad_risk_percent_is_rejected(self, bad):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=bad, price=100.0, atr=2.0)
        assert result["allowed"] is False

    def test_nan_atr_specifically_does_not_slip_through_the_le_zero_check(self):
        """The exact defect: NaN <= 0 is False, so the old guard let it pass."""
        assert (NAN <= 0) is False  # documents the language behavior being defended against

        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=NAN)

        assert result["allowed"] is False
        assert "position_size" not in result


class TestCalculateByStopRejectsNonFiniteInputs:

    @pytest.mark.parametrize("bad", [NAN, INF, NEG_INF, None, True])
    def test_bad_entry_is_rejected(self, bad):
        result = RiskEngine.calculate_by_stop(balance=1000.0, risk_percent=0.1, entry=bad, stop=98.0)
        assert result["allowed"] is False

    @pytest.mark.parametrize("bad", [NAN, INF, NEG_INF, None, True])
    def test_bad_stop_is_rejected(self, bad):
        result = RiskEngine.calculate_by_stop(balance=1000.0, risk_percent=0.1, entry=100.0, stop=bad)
        assert result["allowed"] is False

    def test_nan_stop_would_have_produced_nan_stop_distance(self):
        """
        abs(100.0 - nan) is nan, and `nan <= 0` is False -- so the old
        code computed position_size = risk_amount / nan = nan and returned
        allowed=True.
        """
        result = RiskEngine.calculate_by_stop(balance=1000.0, risk_percent=0.1, entry=100.0, stop=NAN)

        assert result["allowed"] is False
        assert "position_size" not in result


class TestValidInputsStillWork:
    """The guards must not break legitimate calculations."""

    def test_calculate_normal_case(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=2.0)

        assert result["allowed"] is True
        assert result["risk_amount"] == 1.0          # 1000 * 0.1/100
        assert result["position_size"] == 0.5         # 1.0 / 2.0

    def test_calculate_by_stop_normal_case(self):
        result = RiskEngine.calculate_by_stop(balance=1000.0, risk_percent=0.1, entry=100.0, stop=98.0)

        assert result["allowed"] is True
        assert result["risk_amount"] == 1.0
        assert result["position_size"] == 0.5         # 1.0 / |100-98|

    def test_zero_atr_still_rejected_with_original_reason(self):
        result = RiskEngine.calculate(balance=1000.0, risk_percent=0.1, price=100.0, atr=0.0)
        assert result["allowed"] is False
        assert result["reason"] == "ATR is zero"

    def test_zero_stop_distance_still_rejected_with_original_reason(self):
        result = RiskEngine.calculate_by_stop(balance=1000.0, risk_percent=0.1, entry=100.0, stop=100.0)
        assert result["allowed"] is False
        assert result["reason"] == "Stop distance is zero"
