import math

import pytest

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.indicator_step import IndicatorStep


def build_valid_context(candle_count: int = 200) -> MarketContext:
    closes = [
        10000.0 + index
        for index in range(candle_count)
    ]
    highs = [
        close + 10.0
        for close in closes
    ]
    lows = [
        close - 10.0
        for close in closes
    ]

    context = MarketContext(
        symbol="BTC/USDT",
        exchange="binance",
        timeframe="5m",
    )

    context.market = {
        "highs": highs,
        "lows": lows,
        "closes": closes,
    }

    return context


def test_indicator_step_calculates_all_indicators() -> None:
    context = build_valid_context()
    step = IndicatorStep()

    result = step.execute(context)

    assert result is context

    assert "ema" in context.indicators
    assert "rsi" in context.indicators
    assert "atr" in context.indicators
    assert "structure" in context.indicators

    assert context.indicators["ema"]["trend"] in {
        "BULLISH",
        "BEARISH",
        "RANGE",
    }

    assert math.isfinite(
        context.indicators["rsi"]["value"]
    )
    assert math.isfinite(
        context.indicators["atr"]["value"]
    )

    assert context.audit["indicator_step"] == {
        "status": "OK",
        "version": "2.0.0",
        "candles_processed": 200,
    }


def test_missing_market_field_is_rejected() -> None:
    context = build_valid_context()
    del context.market["closes"]

    with pytest.raises(
        ValueError,
        match="missing market field: closes",
    ):
        IndicatorStep().execute(context)


def test_insufficient_candles_are_rejected() -> None:
    context = build_valid_context(candle_count=199)

    with pytest.raises(
        ValueError,
        match="requires at least 200 candles",
    ):
        IndicatorStep().execute(context)


def test_unequal_market_array_lengths_are_rejected() -> None:
    context = build_valid_context()
    context.market["highs"].append(12000.0)

    with pytest.raises(
        ValueError,
        match="must have equal lengths",
    ):
        IndicatorStep().execute(context)


def test_invalid_numeric_data_is_rejected() -> None:
    context = build_valid_context()
    context.market["closes"][50] = float("nan")

    with pytest.raises(
        ValueError,
        match="contains invalid numeric data",
    ):
        IndicatorStep().execute(context)


def test_boolean_market_value_is_rejected() -> None:
    context = build_valid_context()
    context.market["highs"][25] = True

    with pytest.raises(
        ValueError,
        match="contains invalid numeric data",
    ):
        IndicatorStep().execute(context)


def test_high_below_low_is_rejected() -> None:
    context = build_valid_context()

    context.market["highs"][10] = 9000.0
    context.market["lows"][10] = 10000.0
    context.market["closes"][10] = 9500.0

    with pytest.raises(
        ValueError,
        match="high cannot be below low",
    ):
        IndicatorStep().execute(context)


def test_close_outside_candle_range_is_rejected() -> None:
    context = build_valid_context()

    context.market["closes"][10] = (
        context.market["highs"][10] + 1.0
    )

    with pytest.raises(
        ValueError,
        match="close must be between low and high",
    ):
        IndicatorStep().execute(context)