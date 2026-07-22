import pytest

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.paper_execution_step import (
    PaperExecutionStep,
)


def build_context(
    decision: str = "TRADE",
    signal: str = "BUY",
    trade_plan_allowed: bool = True,
) -> MarketContext:
    context = MarketContext()

    context.exchange = "binance"
    context.symbol = "BTCUSDT"
    context.timeframe = "5m"

    context.strategy = {
        "signal": signal,
    }

    context.decision = {
        "decision": decision,
        "reason": (
            "All checks approved"
            if decision == "TRADE"
            else "Trade was blocked"
        ),
    }

    context.execution = {
        "runtime": {
            "mode": "DRY_RUN",
            "real_orders_enabled": False,
        },
        "trade_plan": {
            "allowed": trade_plan_allowed,
            "signal": signal,
            "entry": 100000.0,
            "stop": 99500.0,
            "take_profit_1": 101000.0,
            "take_profit_2": 101500.0,
            "position_size": 0.002,
            "risk_amount": 1.0,
            "execution_mode": "SPOT_LONG_ONLY",
        },
    }

    return context


def test_trade_creates_simulated_filled_order() -> None:
    context = build_context()

    result = PaperExecutionStep().execute(context)

    assert result is context

    order = context.execution["paper_order"]

    assert order == {
        "mode": "PAPER",
        "status": "FILLED_SIMULATED",
        "real_order_sent": False,
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "side": "BUY",
        "entry": 100000.0,
        "quantity": 0.002,
        "stop": 99500.0,
        "take_profit_1": 101000.0,
        "take_profit_2": 101500.0,
        "risk_amount": 1.0,
        "execution_mode": "SPOT_LONG_ONLY",
        "reason": "Virtual paper order executed",
    }

    assert context.audit["paper_execution_step"] == {
        "status": "OK",
        "version": "1.0.0",
        "mode": "PAPER",
        "result": "FILLED_SIMULATED",
        "real_order_sent": False,
    }


def test_no_trade_creates_skipped_order() -> None:
    context = build_context(
        decision="NO_TRADE",
        trade_plan_allowed=False,
    )

    result = PaperExecutionStep().execute(context)

    assert result is context

    order = context.execution["paper_order"]

    assert order == {
        "mode": "PAPER",
        "status": "SKIPPED",
        "real_order_sent": False,
        "reason": "Trade was blocked",
    }

    assert context.audit["paper_execution_step"] == {
        "status": "OK",
        "version": "1.0.0",
        "mode": "PAPER",
        "result": "SKIPPED",
        "real_order_sent": False,
    }


def test_invalid_final_decision_is_rejected() -> None:
    context = build_context()
    context.decision["decision"] = "WAIT"

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep invalid final decision: WAIT"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_non_dictionary_decision_is_rejected() -> None:
    context = build_context()
    context.decision = "TRADE"  # type: ignore[assignment]

    with pytest.raises(
        TypeError,
        match=(
            "PaperExecutionStep expected "
            "context.decision to be dict"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_sell_signal_is_rejected() -> None:
    context = build_context(signal="SELL")

    with pytest.raises(
        ValueError,
        match="PaperExecutionStep supports BUY only",
    ):
        PaperExecutionStep().execute(context)


def test_disallowed_trade_plan_is_rejected() -> None:
    context = build_context(
        trade_plan_allowed=False,
    )

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep trade plan is not allowed"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_missing_trade_plan_field_is_rejected() -> None:
    context = build_context()

    del context.execution["trade_plan"]["position_size"]

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep trade plan missing field: "
            "position_size"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_zero_position_size_is_rejected() -> None:
    context = build_context()

    context.execution["trade_plan"]["position_size"] = 0.0

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep trade plan field "
            "'position_size' must be a positive finite number"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_inconsistent_buy_levels_are_rejected() -> None:
    context = build_context()

    context.execution["trade_plan"]["stop"] = 100500.0

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep BUY levels are inconsistent"
        ),
    ):
        PaperExecutionStep().execute(context)