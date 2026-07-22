import pytest

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.risk_step import RiskStep
from api.risk_engine import RiskEngine


def build_context(
    signal: str = "BUY",
    price: float = 100000.0,
    atr: float = 500.0,
    balance: float = 1000.0,
    risk_percent: float = 0.1,
) -> MarketContext:
    context = MarketContext()

    context.market = {
        "price": price,
    }

    context.indicators = {
        "atr": {
            "value": atr,
        },
    }

    context.strategy = {
        "signal": signal,
    }

    context.portfolio = {
        "balance": balance,
        "risk_percent": risk_percent,
    }

    return context


def test_buy_signal_is_approved() -> None:
    context = build_context()

    result = RiskStep().execute(context)

    assert result is context
    assert context.risk["allowed"] is True
    assert context.risk["risk_amount"] == 1.0
    assert context.risk["position_size"] == 0.002
    assert context.risk["stop_distance"] == 500.0
    assert context.risk["signal"] == "BUY"
    assert context.risk["execution_mode"] == "SPOT_LONG_ONLY"

    assert context.audit["risk_step"] == {
        "status": "OK",
        "version": "2.1.0",
        "allowed": True,
        "reason": "Risk approved",
    }


def test_no_trade_signal_is_blocked() -> None:
    context = build_context(signal="NO TRADE")

    RiskStep().execute(context)

    assert context.risk["allowed"] is False
    assert context.risk["position_size"] == 0.0
    assert context.risk["reason"] == "Strategy returned NO TRADE"


def test_sell_signal_is_blocked_in_spot_long_only_mode() -> None:
    context = build_context(signal="SELL")

    RiskStep().execute(context)

    assert context.risk["allowed"] is False
    assert context.risk["position_size"] == 0.0
    assert (
        context.risk["reason"]
        == "SELL is disabled in SPOT_LONG_ONLY mode"
    )


def test_default_portfolio_values_are_used() -> None:
    context = build_context()
    context.portfolio = {}

    RiskStep().execute(context)

    assert context.risk["balance"] == 1000.0
    assert context.risk["risk_percent"] == 0.1
    assert context.risk["risk_amount"] == 1.0


def test_missing_price_is_rejected() -> None:
    context = build_context()
    del context.market["price"]

    with pytest.raises(
        ValueError,
        match="market price is missing",
    ):
        RiskStep().execute(context)


def test_zero_atr_is_rejected() -> None:
    context = build_context(atr=0.0)

    with pytest.raises(
        ValueError,
        match="ATR must be greater than zero",
    ):
        RiskStep().execute(context)


def test_invalid_strategy_signal_is_rejected() -> None:
    context = build_context(signal="UNKNOWN")

    with pytest.raises(
        ValueError,
        match="invalid strategy signal: UNKNOWN",
    ):
        RiskStep().execute(context)


def test_excessive_risk_percent_is_rejected() -> None:
    context = build_context(risk_percent=0.2)

    with pytest.raises(
        ValueError,
        match="risk percent exceeds maximum 0.1%",
    ):
        RiskStep().execute(context)


def test_non_dictionary_risk_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        RiskEngine,
        "calculate",
        staticmethod(lambda **_: "invalid"),
    )

    with pytest.raises(
        TypeError,
        match=r"RiskEngine.calculate\(\) must return dict",
    ):
        RiskStep().execute(context)


def test_incomplete_risk_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        RiskEngine,
        "calculate",
        staticmethod(
            lambda **_: {
                "allowed": True,
                "risk_amount": 1.0,
            }
        ),
    )

    with pytest.raises(
        ValueError,
        match="RiskEngine result missing field: position_size",
    ):
        RiskStep().execute(context)
