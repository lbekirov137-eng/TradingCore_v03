from api.market_data.candle_utils import (
    drop_unclosed_candle,
    validate_candles,
    StaleMarketDataError,
)

import pytest


FIVE_MIN = 5 * 60 * 1000


def _candles(timestamps, closes=None):
    n = len(timestamps)
    closes = closes or [100.0] * n
    return {
        "timestamps": timestamps,
        "opens": list(closes),
        "highs": [c + 0.1 for c in closes],
        "lows": [c - 0.1 for c in closes],
        "closes": closes,
        "volumes": [10.0] * n,
    }


class TestDropUnclosedCandle:

    def test_drops_last_candle_when_still_forming(self):
        """
        Регрессионный тест на CONFIRMED CRITICAL находку аудита: Binance и
        Bybit возвращают последней ещё не закрытую свечу (подтверждено
        эмпирически против живого API перед фиксом). Если этот тест начнёт
        падать, значит фильтрация вернувшейся "живой" свечи снова сломана.
        """
        now = 10_000_000
        timestamps = [now - 3 * FIVE_MIN, now - 2 * FIVE_MIN, now - FIVE_MIN + 1]
        candles = _candles(timestamps)

        result = drop_unclosed_candle(candles, "5m", now_ms=now)

        assert len(result["timestamps"]) == 2
        assert result["timestamps"] == timestamps[:2]

    def test_keeps_last_candle_when_fully_closed(self):
        now = 10_000_000
        timestamps = [now - 3 * FIVE_MIN, now - 2 * FIVE_MIN, now - 2 * FIVE_MIN + 1]
        # last candle's close time (open + interval) is now - FIVE_MIN + 1, safely in the past
        candles = _candles(timestamps)

        result = drop_unclosed_candle(candles, "5m", now_ms=now)

        assert len(result["timestamps"]) == 3

    def test_empty_input_is_safe(self):
        candles = _candles([])
        result = drop_unclosed_candle(candles, "5m", now_ms=10_000_000)
        assert result["timestamps"] == []


class TestValidateCandles:

    def test_valid_series_passes(self):
        timestamps = [0, FIVE_MIN, 2 * FIVE_MIN]
        candles = _candles(timestamps, closes=[100.0, 100.5, 101.0])
        validate_candles(candles, "5m")  # should not raise

    def test_rejects_duplicate_timestamp(self):
        timestamps = [0, FIVE_MIN, FIVE_MIN]
        candles = _candles(timestamps)
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_out_of_order_timestamp(self):
        timestamps = [0, 2 * FIVE_MIN, FIVE_MIN]
        candles = _candles(timestamps)
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_gap_missing_candle(self):
        timestamps = [0, FIVE_MIN, 3 * FIVE_MIN]  # skipped one 5m candle
        candles = _candles(timestamps)
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_non_positive_price(self):
        timestamps = [0, FIVE_MIN]
        candles = _candles(timestamps, closes=[100.0, 0.0])
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_negative_volume(self):
        timestamps = [0, FIVE_MIN]
        candles = _candles(timestamps)
        candles["volumes"][1] = -1.0
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_nan_price(self):
        timestamps = [0, FIVE_MIN]
        candles = _candles(timestamps)
        candles["closes"][1] = float("nan")
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_low_greater_than_high(self):
        timestamps = [0, FIVE_MIN]
        candles = _candles(timestamps)
        candles["lows"][1] = candles["highs"][1] + 1
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_close_outside_high_low_range(self):
        timestamps = [0, FIVE_MIN]
        candles = _candles(timestamps)
        candles["closes"][1] = candles["highs"][1] + 5
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")

    def test_rejects_empty_series(self):
        candles = _candles([])
        with pytest.raises(StaleMarketDataError):
            validate_candles(candles, "5m")
