import pytest

from api.contracts.context import MarketContext
from api.decision_engine.decision_engine import DecisionEngine
from api.pipeline_v2.steps.decision_step import DecisionStep


def build_context(
    signal: str = "BUY",
    risk_allowed: bool = True,
    trade_plan_allowed: bool = True,
) -> MarketContext:
    context = MarketContext()

    context.strategy = {
        "signal": signal,
    }

    context.risk = {
        "allowed": risk_allowed,
    }

    context.execution = {
        "trade_plan": {
            "allowed": trade_plan_allowed,
        },
    }

    return context


def set_engine_decision(
    context: MarketContext,
    decision: str = "TRADE",
    score: int = 50,
    confidence: float = 0.9,
    failed_rules: list[str] | None = None,
    reason: str = "Score evaluation",
) -> MarketContext:
    context.decision = {
        "decision": decision,
        "score": score,
        "confidence": confidence,
        "failed_rules": (
            failed_rules
            if failed_rules is not None
            else []
        ),
        "reason": reason,
    }

    return context


def test_all_approvals_produce_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    result = DecisionStep().execute(context)

    assert result is context
    assert context.decision["decision"] == "TRADE"
    assert context.decision["engine_decision"] == "TRADE"
    assert context.decision["signal"] == "BUY"
    assert context.decision["risk_allowed"] is True
    assert context.decision["trade_plan_allowed"] is True
    assert (
        context.decision["execution_mode"]
        == "SPOT_LONG_ONLY"
    )

    assert context.audit["decision_step"] == {
        "status": "OK",
        "version": "2.1.0",
        "decision": "TRADE",
        "reason": (
            "Strategy, risk, trade plan and "
            "decision rules approved"
        ),
    }


def test_non_buy_signal_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(signal="NO TRADE")

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "Strategy signal is not BUY: NO TRADE"
        in context.decision["reason"]
    )


def test_risk_rejection_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(risk_allowed=False)

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "RiskStep did not approve the trade"
        in context.decision["reason"]
    )


def test_trade_plan_rejection_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(
        trade_plan_allowed=False,
    )

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "TradePlanStep did not approve the trade plan"
        in context.decision["reason"]
    )


def test_engine_no_trade_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="NO_TRADE",
                reason="Critical rule failed",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "DecisionEngine blocked trade: Critical rule failed"
        in context.decision["reason"]
    )


def test_non_context_engine_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(lambda _: "invalid"),
    )

    with pytest.raises(
        TypeError,
        match=(
            r"DecisionEngine.process\(\) "
            r"must return MarketContext"
        ),
    ):
        DecisionStep().execute(context)


def test_non_dictionary_decision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    def invalid_process(
        current_context: MarketContext,
    ) -> MarketContext:
        current_context.decision = "invalid"
        return current_context

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(invalid_process),
    )

    with pytest.raises(
        TypeError,
        match="DecisionEngine result must be dict",
    ):
        DecisionStep().execute(context)


def test_missing_decision_field_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    def incomplete_process(
        current_context: MarketContext,
    ) -> MarketContext:
        current_context.decision = {
            "decision": "TRADE",
            "score": 50,
            "confidence": 0.9,
            "reason": "Score evaluation",
        }

        return current_context

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(incomplete_process),
    )

    with pytest.raises(
        ValueError,
        match=(
            "DecisionEngine result missing field: "
            "failed_rules"
        ),
    ):
        DecisionStep().execute(context)


def test_unknown_engine_decision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="WAIT",
            )
        ),
    )

    with pytest.raises(
        ValueError,
        match=(
            "DecisionEngine returned invalid decision: WAIT"
        ),
    ):
        DecisionStep().execute(context)


def test_invalid_confidence_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                confidence=1.5,
            )
        ),
    )

    with pytest.raises(
        ValueError,
        match=(
            "DecisionEngine confidence must be "
            "between 0 and 1"
        ),
    ):
        DecisionStep().execute(context)