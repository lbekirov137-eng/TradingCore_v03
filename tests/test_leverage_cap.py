"""
Locks the leverage cap at 1x for the first cloud PAPER run.

The 2x/3x tiering logic in LeverageRiskEngine is deliberately preserved
(not deleted) but made structurally unreachable via
MAX_LEVERAGE = 1, since the final value is
min(maximum_allowed_leverage, MAX_LEVERAGE).

These tests use inputs that WOULD otherwise qualify for the 3x tier
(regime aligned, signal_quality >= 0.90, liquidity >= 0.80,
volatility <= 0.40, stop_distance_percent <= 0.015). If someone raises
MAX_LEVERAGE without an explicit risk-policy decision, these fail.
"""

import pytest

from api.leverage_risk_engine import LeverageRiskEngine


def _three_x_qualifying_inputs(**overrides):
    """Inputs that satisfy every condition of the 3x tier."""
    base = dict(
        capital=1000.0,
        entry_price=100.0,
        stop_price=99.0,          # 1% stop distance -> <= 0.015
        side="BUY",
        signal_quality=0.95,       # >= 0.90
        volatility=0.20,           # <= 0.40
        liquidity=0.90,            # >= 0.80
        market_regime="TREND_UP",
        data_ok=True,
        system_ok=True,
    )
    base.update(overrides)
    return base


class TestLeverageIsCappedAtOne:

    def test_max_leverage_constant_is_one(self):
        assert LeverageRiskEngine.MAX_LEVERAGE == 1, (
            "MAX_LEVERAGE must stay 1 for the first PAPER run. Raising it "
            "is a risk-policy decision, not a routine change."
        )

    def test_three_x_qualifying_setup_still_yields_at_most_1x(self):
        result = LeverageRiskEngine.evaluate(**_three_x_qualifying_inputs())

        leverage = result.get("leverage") or result.get("selected_leverage")

        if leverage is not None:
            assert leverage <= 1, f"Leverage {leverage} exceeded the 1x cap"

    def test_notional_never_exceeds_capital(self):
        """
        The practical meaning of 1x: position notional must never exceed
        available capital, i.e. no borrowed exposure.
        """
        result = LeverageRiskEngine.evaluate(**_three_x_qualifying_inputs())

        notional = result.get("position_notional")
        if notional is not None:
            assert notional <= 1000.0 + 1e-9, (
                f"Notional {notional} exceeds capital 1000.0 -- that is leverage > 1x"
            )

    def test_risk_percent_ceiling_is_still_point_one_percent(self):
        assert LeverageRiskEngine.MAX_RISK_PERCENT == 0.001, (
            "risk_per_trade must remain 0.001 (0.1%) or lower"
        )

    def test_no_real_order_is_ever_flagged_as_sent(self):
        result = LeverageRiskEngine.evaluate(**_three_x_qualifying_inputs())
        assert result.get("real_order_sent") is False


class TestLeverageEngineStillRejectsUnsafeInputs:
    """The cap must not mask the engine's own safety rejections."""

    def test_unreliable_data_is_rejected(self):
        result = LeverageRiskEngine.evaluate(**_three_x_qualifying_inputs(data_ok=False))
        assert result.get("real_order_sent") is False
        assert result.get("signal") == "NO_TRADE" or "reason" in result

    def test_unhealthy_system_is_rejected(self):
        result = LeverageRiskEngine.evaluate(**_three_x_qualifying_inputs(system_ok=False))
        assert result.get("real_order_sent") is False

    def test_invalid_capital_is_rejected(self):
        result = LeverageRiskEngine.evaluate(**_three_x_qualifying_inputs(capital=0))
        assert result.get("real_order_sent") is False
