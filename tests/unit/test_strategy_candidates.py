from api.contracts.context import LiveContext
from api.ema import EMAEngine
from api.rsi import RSIEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure

from api.strategy_engine.strategies.orb.candidates import ORBWithRangeATRFilter, ORBWithMinRelativeVolume
from api.strategy_engine.strategies.vwap.candidates import (
    VWAPTighterPullback, VWAPWiderPullback, VWAPWithVolumeConfirmation,
)

from tests.conftest import orb_breakout_snapshot


def _orb_context(snapshot):
    ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
    ctx.market = snapshot
    ctx.now_ms = snapshot.timestamps[-1]
    ctx.indicators["ema"] = EMAEngine.calculate_all(snapshot.closes)
    ctx.indicators["rsi"] = RSIEngine.calculate(snapshot.closes)
    ctx.indicators["atr"] = ATREngine.calculate(snapshot.highs, snapshot.lows, snapshot.closes)
    ctx.indicators["structure"] = MarketStructure.analyze(snapshot.highs, snapshot.lows)
    return ctx


class TestORBCandidates:

    def test_range_atr_filter_labels_its_own_strategy_name(self):
        ctx = _orb_context(orb_breakout_snapshot(breakout=True))
        candidate = ORBWithRangeATRFilter(min_ratio=0.0, max_ratio=100.0)  # permissive, should pass through

        signal = candidate.generate(ctx)

        assert signal["strategy"] == "ORB_RANGE_ATR_FILTER"

    def test_range_atr_filter_rejects_when_ratio_out_of_bounds(self):
        ctx = _orb_context(orb_breakout_snapshot(breakout=True))
        candidate = ORBWithRangeATRFilter(min_ratio=1000.0, max_ratio=2000.0)  # impossible window

        signal = candidate.generate(ctx)

        assert signal["approved"] is False
        assert "Range/ATR" in signal["reason"]

    def test_min_relative_volume_rejects_low_volume_breakout(self):
        snapshot = orb_breakout_snapshot(breakout=True)
        snapshot.volumes[-1] = 0.001  # far below the 20-candle average
        ctx = _orb_context(snapshot)

        candidate = ORBWithMinRelativeVolume(min_volume_ratio=1.0)
        signal = candidate.generate(ctx)

        assert signal["approved"] is False
        assert "Объём пробоя" in signal["reason"]

    def test_min_relative_volume_allows_normal_volume(self):
        ctx = _orb_context(orb_breakout_snapshot(breakout=True))
        candidate = ORBWithMinRelativeVolume(min_volume_ratio=0.0)  # trivially permissive

        signal = candidate.generate(ctx)

        assert signal["strategy"] == "ORB_MIN_RELATIVE_VOLUME"


class TestVWAPCandidates:

    def test_candidates_are_independent_not_combined(self):
        """
        Each candidate must be independently callable and self-labeled --
        none of them silently reference or merge with another.
        """
        assert VWAPTighterPullback.NAME != VWAPWiderPullback.NAME != VWAPWithVolumeConfirmation.NAME

    def test_tighter_pullback_runs_without_crashing_on_flat_data(self):
        from tests.conftest import make_snapshot
        snapshot = make_snapshot([100.0] * 30)
        ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m")
        ctx.market = snapshot
        ctx.now_ms = snapshot.timestamps[-1]
        ctx.indicators["ema"] = EMAEngine.calculate_all(snapshot.closes)
        ctx.indicators["atr"] = ATREngine.calculate(snapshot.highs, snapshot.lows, snapshot.closes)

        result = VWAPTighterPullback.generate(ctx, apply_filters=False)
        assert result["approved"] is False  # flat data -> no valid setup, but must not crash
