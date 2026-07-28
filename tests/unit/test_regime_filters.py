import time

import pytest

from api.contracts.context import LiveContext
from api.strategy_engine.filters.regime import (
    Regime, classify_regime, check_data_quality, check_liquidity,
    check_spread, evaluate_all,
)
from api.ema import EMAEngine
from api.atr import ATREngine

from tests.conftest import make_snapshot


def _context(snapshot, now_ms=None):
    ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
    ctx.market = snapshot
    ctx.now_ms = now_ms if now_ms is not None else snapshot.timestamps[-1]
    ctx.indicators["ema"] = EMAEngine.calculate_all(snapshot.closes)
    ctx.indicators["atr"] = ATREngine.calculate(snapshot.highs, snapshot.lows, snapshot.closes)
    return ctx


def _trend_up(n=60):
    closes = [100.0 + i * 0.25 for i in range(n)]
    return make_snapshot(closes, highs=[c + 0.3 for c in closes], lows=[c - 0.3 for c in closes])


def _flat(n=60):
    closes = [100.0 + (0.02 if i % 2 == 0 else -0.02) for i in range(n)]
    return make_snapshot(closes, highs=[c + 0.03 for c in closes], lows=[c - 0.03 for c in closes])


class TestDataQuality:

    def test_fresh_data_passes(self):
        snapshot = _trend_up()
        result = check_data_quality(_context(snapshot))
        assert result.allowed is True

    def test_stale_data_is_rejected(self):
        snapshot = _trend_up()
        ctx = _context(snapshot, now_ms=snapshot.timestamps[-1] + 10_000_000)
        result = check_data_quality(ctx)
        assert result.allowed is False
        assert "устарели" in result.reason

    def test_empty_market_is_rejected(self):
        snapshot = make_snapshot([])
        ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m")
        ctx.market = snapshot
        result = check_data_quality(ctx, now=time.time())
        assert result.allowed is False

    def test_abnormal_candle_move_is_rejected(self):
        closes = [100.0] * 30 + [200.0]  # +100% in one candle
        snapshot = make_snapshot(closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes])
        result = check_data_quality(_context(snapshot))
        assert result.allowed is False
        assert "Аномальное" in result.reason


class TestLiquidity:

    def test_normal_volume_passes(self):
        snapshot = _trend_up()
        result = check_liquidity(_context(snapshot))
        assert result.allowed is True

    def test_low_volume_is_rejected(self):
        snapshot = _trend_up()
        snapshot.volumes[-1] = 0.01  # far below the 20-period average
        result = check_liquidity(_context(snapshot))
        assert result.allowed is False
        assert "ликвидность" in result.reason.lower()

    def test_zero_average_volume_is_rejected(self):
        snapshot = _trend_up()
        snapshot.volumes = [0.0] * len(snapshot.volumes)
        result = check_liquidity(_context(snapshot))
        assert result.allowed is False

    def test_insufficient_history_is_rejected(self):
        snapshot = make_snapshot([100.0] * 5)
        result = check_liquidity(_context(snapshot))
        assert result.allowed is False


class TestSpread:

    def test_none_spread_allowed_in_paper_mode(self):
        assert check_spread(None).allowed is True

    def test_wide_spread_rejected(self):
        assert check_spread(5.0).allowed is False

    def test_narrow_spread_allowed(self):
        assert check_spread(0.01).allowed is True

    def test_negative_spread_rejected(self):
        assert check_spread(-1.0).allowed is False


class TestRegimeClassification:

    def test_uptrend_is_detected(self):
        assert classify_regime(_context(_trend_up())) == Regime.TREND_UP

    def test_insufficient_data_is_undetermined(self):
        snapshot = make_snapshot([100.0] * 5)
        assert classify_regime(_context(snapshot)) == Regime.UNDETERMINED

    def test_flat_market_is_low_volatility_or_range(self):
        regime = classify_regime(_context(_flat()))
        assert regime in (Regime.LOW_VOLATILITY, Regime.RANGE, Regime.UNDETERMINED)


class TestEvaluateAll:
    """
    Ключевой инвариант: неопределённый режим НЕ разрешает торговлю.
    """

    def test_undetermined_regime_blocks_trading(self):
        """
        Если индикаторы недоступны (сбой расчёта, недостаток истории),
        режим не определён — торговля обязана быть заблокирована, а не
        разрешена «по умолчанию».
        """
        snapshot = _trend_up()
        ctx = _context(snapshot)
        ctx.indicators["atr"] = {"value": float("nan")}  # simulate indicator failure

        result = evaluate_all(ctx)

        assert result.allowed is False
        assert result.regime == Regime.UNDETERMINED

    def test_range_regime_is_determined_and_allowed_for_breakout_setups(self):
        """
        RANGE — это ОПРЕДЕЛЁННЫЙ режим (и естественная предпосылка для
        ORB-пробоя), поэтому он не блокируется. Блокируется только
        UNDETERMINED и экстремальная/недостаточная волатильность.
        """
        snapshot = make_snapshot([100.0] * 25)
        result = evaluate_all(_context(snapshot))

        assert result.regime in (Regime.RANGE, Regime.LOW_VOLATILITY)
        if result.regime == Regime.RANGE:
            assert result.allowed is True

    def test_stale_data_blocks_before_regime_is_even_considered(self):
        snapshot = _trend_up()
        ctx = _context(snapshot, now_ms=snapshot.timestamps[-1] + 10_000_000)
        result = evaluate_all(ctx)
        assert result.allowed is False
        assert "устарели" in result.reason

    def test_healthy_uptrend_passes_all_filters(self):
        result = evaluate_all(_context(_trend_up()))
        assert result.allowed is True
        assert result.regime == Regime.TREND_UP

    def test_result_is_serializable(self):
        result = evaluate_all(_context(_trend_up()))
        payload = result.to_dict()
        assert set(payload) == {"allowed", "reason", "regime", "details"}
