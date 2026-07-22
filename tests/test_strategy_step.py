import pytest

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.strategy_step import StrategyStep
from api.signal_engine import SignalEngine


def build_context(
    trend: str = "BULLISH",
    structure: str = "UPTREND",
    rsi_value: float = 60.0,
) -> MarketContext:
    context = MarketContext()
    context.indicators = {
        "ema": {"trend": trend},
        "rsi": {"value": rsi_value},
        "structure": {"structure": structure},
    }
    return context


def test_buy_signal() -> None:
    context = build_context()
    StrategyStep().execute(context)

    assert context.strategy == {"signal": "BUY"}
    assert context.audit["strategy_step"]["signal"] == "BUY"


def test_sell_signal() -> None:
    context = build_context(
        trend="BEARISH",
        structure="DOWNTREND",
        rsi_value=40.0,
    )
    StrategyStep().execute(context)

    assert context.strategy == {"signal": "SELL"}


def test_no_trade_signal() -> None:
    context = build_context(rsi_value=70.0)
    StrategyStep().execute(context)

    assert context.strategy == {"signal": "NO TRADE"}


def test_missing_indicator_is_rejected() -> None:
    context = build_context()
    del context.indicators["rsi"]

    with pytest.raises(ValueError, match="missing indicator: rsi"):
        StrategyStep().execute(context)


def test_invalid_rsi_is_rejected() -> None:
    context = build_context(rsi_value=101.0)

    with pytest.raises(ValueError, match="RSI must be between 0 and 100"):
        StrategyStep().execute(context)


def test_invalid_signal_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        SignalEngine,
        "generate",
        staticmethod(lambda **_: {"signal": "UNKNOWN"}),
    )

    with pytest.raises(
        ValueError,
        match="SignalEngine returned invalid signal: UNKNOWN",
    ):
        StrategyStep().execute(context)
