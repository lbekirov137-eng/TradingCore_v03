from datetime import datetime as RealDateTime
from datetime import timezone

import pytest

from api.contracts.context import MarketContext
from api.decision_engine.rules import session_rule as session_rule_module
from api.decision_engine.rules.session_rule import SessionRule


def build_context(
    mode: str = "DRY_RUN",
    utc_hour_override: int = 12,
) -> MarketContext:
    context = MarketContext()

    context.execution = {
        "runtime": {
            "mode": mode,
            "utc_hour_override": utc_hour_override,
            "real_orders_enabled": False,
        },
    }

    return context


def test_active_dry_run_override_passes() -> None:
    context = build_context(
        mode="DRY_RUN",
        utc_hour_override=12,
    )

    result = SessionRule().evaluate(context)

    assert result["passed"] is True
    assert result["critical"] is False
    assert result["score"] == 15
    assert result["confidence"] == 0.95
    assert result["reason"] == "Активная торговая сессия"


def test_inactive_dry_run_override_blocks() -> None:
    context = build_context(
        mode="DRY_RUN",
        utc_hour_override=3,
    )

    result = SessionRule().evaluate(context)

    assert result["passed"] is False
    assert result["critical"] is True
    assert result["score"] == 0
    assert result["confidence"] == 1.0
    assert result["reason"] == "Вне активной торговой сессии"


def test_invalid_dry_run_hour_is_rejected() -> None:
    context = build_context(
        mode="DRY_RUN",
        utc_hour_override=24,
    )

    with pytest.raises(
        ValueError,
        match=(
            "SessionRule utc_hour_override "
            "must be between 0 and 23"
        ),
    ):
        SessionRule().evaluate(context)


def test_boolean_dry_run_hour_is_rejected() -> None:
    context = build_context(
        mode="DRY_RUN",
        utc_hour_override=True,
    )

    with pytest.raises(
        TypeError,
        match="SessionRule utc_hour_override must be int",
    ):
        SessionRule().evaluate(context)


def test_override_is_ignored_outside_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(
        mode="PRODUCTION",
        utc_hour_override=12,
    )

    class FakeDateTime:
        @classmethod
        def now(
            cls,
            tz: timezone | None = None,
        ) -> RealDateTime:
            return RealDateTime(
                2026,
                1,
                1,
                3,
                0,
                0,
                tzinfo=timezone.utc,
            )

    monkeypatch.setattr(
        session_rule_module,
        "datetime",
        FakeDateTime,
    )

    result = SessionRule().evaluate(context)

    assert result["passed"] is False
    assert result["critical"] is True
    assert result["reason"] == "Вне активной торговой сессии"