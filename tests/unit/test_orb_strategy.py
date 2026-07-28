from api.contracts.context import LiveContext
from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy
from api.ema import EMAEngine
from api.rsi import RSIEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure

from tests.conftest import orb_breakout_snapshot, make_snapshot


def _context_from_snapshot(snapshot):
    ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
    ctx.market = snapshot
    # Эти тесты проверяют механику ORB на детерминированных исторических
    # данных, поэтому «сейчас» = время последней свечи.
    ctx.now_ms = snapshot.timestamps[-1]
    ctx.indicators["ema"] = EMAEngine.calculate_all(snapshot.closes)
    ctx.indicators["rsi"] = RSIEngine.calculate(snapshot.closes)
    ctx.indicators["atr"] = ATREngine.calculate(snapshot.highs, snapshot.lows, snapshot.closes)
    ctx.indicators["structure"] = MarketStructure.analyze(snapshot.highs, snapshot.lows)
    return ctx


class TestORBStrategyHappyPath:

    def test_approved_long_breakout_has_consistent_trade_plan(self):
        ctx = _context_from_snapshot(orb_breakout_snapshot(breakout=True))

        signal = ORBStrategy.generate(ctx)

        assert signal["approved"] is True
        assert signal["direction"] == "LONG"

        entry = signal["trade_plan"]["entry"]
        stop = signal["trade_plan"]["stop_loss"]
        tp1 = signal["trade_plan"]["take_profit"]["tp1"]
        tp2 = signal["trade_plan"]["take_profit"]["tp2"]

        assert entry == 100.25
        # Стоп должен быть НИЖЕ входа для LONG, и ниже нижней границы диапазона.
        assert stop < 99.8
        assert stop < entry

        risk = entry - stop
        # Независимый пересчёт TP по формуле TakeProfit.calculate.
        assert tp1 == round(entry + risk * 2, 10) or abs(tp1 - (entry + risk * 2)) < 1e-6
        assert tp2 > tp1


class TestORBStrategyNoTrade:

    def test_no_breakout_gives_no_trade(self):
        ctx = _context_from_snapshot(orb_breakout_snapshot(breakout=False))

        signal = ORBStrategy.generate(ctx)

        assert signal["approved"] is False
        assert signal["direction"] is None

    def test_insufficient_candles_gives_no_trade(self):
        snapshot = make_snapshot([100.0, 100.1, 100.2])  # < 5 candles
        ctx = _context_from_snapshot(snapshot)

        signal = ORBStrategy.generate(ctx)

        assert signal["approved"] is False
        assert "Opening Range" in signal["reason"] or signal["reason"]

    def test_nan_atr_blocks_trade_even_with_valid_breakout(self):
        """
        Regression: ORBStrategy должен сам защититься от NaN/невалидного ATR,
        даже если формально пробой и ретест подтверждены (мало данных для
        rolling(14) ATR — типичный случай сразу после старта/после сбоя).
        """
        snapshot = orb_breakout_snapshot(breakout=True, n_filler=0)  # too few candles for ATR(14)
        ctx = _context_from_snapshot(snapshot)

        # apply_filters=False изолирует именно проверку ATR: иначе первым
        # сработал бы фильтр ликвидности (мало свечей), и тест перестал бы
        # проверять то, ради чего написан.
        signal = ORBStrategy.generate(ctx, apply_filters=False)

        assert signal["approved"] is False
        assert "ATR" in signal["reason"]
