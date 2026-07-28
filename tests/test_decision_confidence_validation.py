"""
Confidence range validation for DecisionStep.

Confirmed Category B defect (PRE_EXISTING_FAILURES_TRIAGE.md #34): the
engine decision was checked only for the PRESENCE of a `confidence`
field, never its value. Values such as 1.5, -3, NaN or Inf passed
silently into decision scoring and into the paper-forward statistics this
stage exists to collect.

These tests exercise the validator directly, so they are independent of
the `selected_trade` contract migration still pending for the wider
pipeline test suite.
"""

import math

import pytest

from api.pipeline_v2.steps.decision_step import DecisionStep


def _validate(confidence):
    """Invoke the validator through the public decision-shaped path."""
    DecisionStep()._validate_engine_decision(
        {
            "decision": "NO_TRADE",
            "score": 0,
            "confidence": confidence,
            "failed_rules": [],
            "reason": "test",
        }
    )


class TestValidConfidenceIsAccepted:

    @pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.75, 1.0])
    def test_values_inside_the_range_pass(self, value):
        _validate(value)  # must not raise

    def test_both_boundaries_are_inclusive(self):
        """0.0 and 1.0 are valid, per the approved decision."""
        _validate(0.0)
        _validate(1.0)

    def test_integer_zero_and_one_are_accepted(self):
        _validate(0)
        _validate(1)


class TestOutOfRangeConfidenceIsRejected:

    @pytest.mark.parametrize("value", [1.5, 2.0, 100, 1.0000001])
    def test_above_one_is_rejected(self, value):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(value)

    @pytest.mark.parametrize("value", [-0.1, -1, -3, -0.0000001])
    def test_below_zero_is_rejected(self, value):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(value)


class TestNonFiniteConfidenceIsRejected:
    """NaN is the important one: `0.0 <= nan <= 1.0` is False, but a naive
    range check written the other way round could let it through."""

    def test_nan_is_rejected(self):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(float("nan"))

    def test_positive_infinity_is_rejected(self):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(float("inf"))

    def test_negative_infinity_is_rejected(self):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(float("-inf"))

    def test_nan_comparison_behaviour_is_documented(self):
        """Documents why an explicit isnan check is required, not redundant."""
        assert (0.0 <= float("nan") <= 1.0) is False
        assert (float("nan") > 1.0) is False


class TestNonNumericConfidenceIsRejected:

    @pytest.mark.parametrize("value", [None, "0.5", [], {}, object()])
    def test_non_numeric_is_rejected(self, value):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(value)

    @pytest.mark.parametrize("value", [True, False])
    def test_booleans_are_rejected(self, value):
        """
        In Python bool is a subclass of int, so True would silently read as
        1.0 and False as 0.0 -- masking a bug in whatever produced it.
        """
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            _validate(value)


class TestExistingValidationsStillWork:
    """The new check must not displace the checks that were already correct."""

    def test_non_dict_decision_still_raises_type_error(self):
        with pytest.raises(TypeError, match="DecisionEngine result must be dict"):
            DecisionStep()._validate_engine_decision(["not", "a", "dict"])

    def test_missing_field_still_raises(self):
        with pytest.raises(ValueError, match="DecisionEngine missing field: failed_rules"):
            DecisionStep()._validate_engine_decision(
                {"decision": "NO_TRADE", "score": 0, "confidence": 0.5, "reason": "x"}
            )

    def test_invalid_decision_value_still_raises(self):
        with pytest.raises(ValueError, match="Invalid DecisionEngine decision"):
            DecisionStep()._validate_engine_decision(
                {
                    "decision": "WAIT",
                    "score": 0,
                    "confidence": 0.5,
                    "failed_rules": [],
                    "reason": "x",
                }
            )
