import math

import pytest
from hypothesis import given, settings, strategies as st

from api.execution.position_sizing import PositionSizer


DEFAULTS = dict(
    balance=1000.0,
    available_balance=1000.0,
    risk_percent=0.1,
    fee_rate=0.001,
    slippage_bps=5.0,
    tick_size=0.01,
    lot_size=0.000001,
    min_notional=5.0,
    max_position_percent_of_balance=100.0,
)


def _calc(**overrides):
    kwargs = {**DEFAULTS, **overrides}
    return PositionSizer.calculate(**kwargs)


class TestIndependentRecalculation:

    def test_matches_hand_derived_formula(self):
        result = _calc(entry=100.0, stop=98.0)

        risk_amount = 1000.0 * (0.1 / 100)  # 1.0
        slippage_amount = 100.0 * (5.0 / 10_000)  # 0.05
        effective_stop_distance = 2.0 + 2 * slippage_amount  # 2.1
        fee_per_unit = 0.001 * (100.0 + 98.0)  # 0.198
        expected_raw_qty = risk_amount / (effective_stop_distance + fee_per_unit)
        expected_qty = math.floor(expected_raw_qty / 0.000001) * 0.000001

        assert result.allowed is True
        assert abs(result.quantity - expected_qty) < 1e-9

    def test_risk_amount_is_point_one_percent_of_balance(self):
        result = _calc(entry=100.0, stop=98.0)
        assert result.risk_amount == 1.0


class TestRejectionCases:

    @pytest.mark.parametrize("field,value", [
        ("balance", 0),
        ("balance", -100),
        ("balance", float("nan")),
        ("balance", float("inf")),
        ("available_balance", 0),
        ("available_balance", -1),
        ("risk_percent", 0),
        ("risk_percent", -0.1),
        ("fee_rate", -0.001),
        ("slippage_bps", -1),
        ("tick_size", 0),
        ("tick_size", -0.01),
        ("lot_size", 0),
        ("min_notional", -1),
        ("max_position_percent_of_balance", 0),
    ])
    def test_rejects_invalid_scalar(self, field, value):
        result = _calc(entry=100.0, stop=98.0, **{field: value})
        assert result.allowed is False

    def test_rejects_none_entry(self):
        result = _calc(entry=None, stop=98.0)
        assert result.allowed is False

    def test_rejects_nan_entry(self):
        result = _calc(entry=float("nan"), stop=98.0)
        assert result.allowed is False

    def test_rejects_infinite_stop(self):
        result = _calc(entry=100.0, stop=float("inf"))
        assert result.allowed is False

    def test_rejects_zero_entry_or_stop(self):
        assert _calc(entry=0.0, stop=98.0).allowed is False
        assert _calc(entry=100.0, stop=0.0).allowed is False

    def test_rejects_negative_entry_or_stop(self):
        assert _calc(entry=-100.0, stop=98.0).allowed is False
        assert _calc(entry=100.0, stop=-98.0).allowed is False

    def test_rejects_entry_equal_stop(self):
        result = _calc(entry=100.0, stop=100.0)
        assert result.allowed is False

    def test_rejects_available_balance_greater_than_balance(self):
        result = _calc(entry=100.0, stop=98.0, balance=500.0, available_balance=1000.0)
        assert result.allowed is False

    def test_rejects_below_minimum_notional(self):
        # Tiny risk budget -> tiny quantity -> notional below min_notional
        result = _calc(entry=100.0, stop=98.0, balance=1.0, available_balance=1.0, min_notional=5.0)
        assert result.allowed is False
        assert "минимального" in result.reason

    def test_rejects_oversized_position_would_require_leverage(self):
        # Huge risk_percent relative to a tiny stop distance -> notional > balance
        result = _calc(entry=100.0, stop=99.99, risk_percent=50.0, balance=1000.0)
        assert result.allowed is False

    def test_rejects_when_position_exceeds_max_percent_of_balance(self):
        result = _calc(
            entry=100.0, stop=95.0, risk_percent=10.0,
            balance=100_000.0, available_balance=100_000.0,
            max_position_percent_of_balance=1.0,
        )
        assert result.allowed is False

    def test_zero_lot_size_quantity_rejected_not_crashed(self):
        # A stop_distance so large relative to risk that quantity floors to 0
        result = _calc(entry=100.0, stop=1.0, risk_percent=0.0001, lot_size=1.0)
        assert result.allowed is False
        assert "нуля" in result.reason


class TestNoLeverageInvariant:

    def test_approved_position_never_exceeds_available_balance(self):
        result = _calc(entry=100.0, stop=98.0)
        assert result.allowed is True
        assert result.notional <= 1000.0


# --- Property-based tests (Hypothesis) -------------------------------------

finite_positive = st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False, allow_infinity=False)


@given(
    entry=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    stop_offset=st.floats(min_value=0.001, max_value=0.5, allow_nan=False, allow_infinity=False),
    balance=st.floats(min_value=1.0, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    risk_percent=st.floats(min_value=0.001, max_value=5.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_property_approved_result_never_exceeds_available_balance(entry, stop_offset, balance, risk_percent):
    stop = entry * (1 - stop_offset)
    if stop <= 0:
        return

    result = PositionSizer.calculate(
        balance=balance,
        available_balance=balance,
        risk_percent=risk_percent,
        entry=entry,
        stop=stop,
        fee_rate=0.001,
        slippage_bps=5.0,
        tick_size=0.01,
        lot_size=0.000001,
        min_notional=0.0,
        max_position_percent_of_balance=100.0,
    )

    if result.allowed:
        assert result.notional <= balance + 1e-6
        assert result.quantity > 0
        assert not math.isnan(result.quantity)
        assert not math.isinf(result.quantity)


@given(
    bad_value=st.one_of(
        st.just(float("nan")),
        st.just(float("inf")),
        st.just(float("-inf")),
        st.floats(max_value=0, allow_nan=False, allow_infinity=False),
    )
)
@settings(max_examples=50)
def test_property_any_bad_balance_is_always_rejected(bad_value):
    result = PositionSizer.calculate(
        balance=bad_value,
        available_balance=1000.0,
        risk_percent=0.1,
        entry=100.0,
        stop=98.0,
        fee_rate=0.001,
        slippage_bps=5.0,
        tick_size=0.01,
        lot_size=0.000001,
        min_notional=5.0,
        max_position_percent_of_balance=100.0,
    )
    assert result.allowed is False
