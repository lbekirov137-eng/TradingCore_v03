"""
CRITICAL confirmed finding: api/strategy_engine/strategies/orb/retest.py и
entry.py читали `context.market` (полный, невидимый на момент решения
набор свечей) вместо `context.visible_market` (обрезанный по индексу набор,
который единственно доступен на момент принятия решения в backtest).
OpeningRange и Breakout всегда использовали visible_market правильно —
несогласованность создавала классический look-ahead bias: цена входа и
условие ретеста могли незаметно "подглядывать" в будущую свечу.

Эти тесты обязаны падать, если кто-то снова заменит visible_market на
market в Retest/Entry.
"""

from api.backtesting.backtest_context import BacktestContext
from api.strategy_engine.strategies.orb.opening_range import OpeningRange
from api.strategy_engine.strategies.orb.breakout import Breakout
from api.strategy_engine.strategies.orb.retest import Retest
from api.strategy_engine.strategies.orb.entry import Entry

from tests.conftest import orb_breakout_snapshot


FUTURE_SPIKE_CLOSE = 105.0

# Цена закрытия последней ВИДИМОЙ свечи в happy-path ORB фикстуре
# (свеча пробоя). Именно она обязана использоваться для входа/ретеста.
EXPECTED_VISIBLE_CLOSE = 100.25


def _full_market_with_future_spike():
    """
    Happy-path ORB фикстура + 1 «будущая» свеча с ценой 105.0, которая
    не должна быть видна решению, принимаемому на последней реальной свече.

    Индекс решения вычисляется от размера фикстуры, а не захардкожен —
    иначе тест ломается при любом изменении длины фикстуры и перестаёт
    проверять то, ради чего написан.
    """

    snapshot = orb_breakout_snapshot(breakout=True)

    decision_index = len(snapshot.timestamps) - 1

    future_ts = snapshot.timestamps[-1] + (snapshot.timestamps[1] - snapshot.timestamps[0])

    snapshot.timestamps.append(future_ts)
    snapshot.opens.append(FUTURE_SPIKE_CLOSE)
    snapshot.highs.append(FUTURE_SPIKE_CLOSE + 0.2)
    snapshot.lows.append(FUTURE_SPIKE_CLOSE - 0.2)
    snapshot.closes.append(FUTURE_SPIKE_CLOSE)
    snapshot.volumes.append(10.0)

    return snapshot, decision_index


def test_retest_ignores_future_candle():
    market, decision_index = _full_market_with_future_spike()

    context = BacktestContext(index=decision_index, market=market, indicators={}, balance=1000.0)

    opening_range = OpeningRange.calculate(context)
    breakout = Breakout.detect(context, opening_range)

    assert breakout["confirmed"] is True  # sanity check on fixture

    retest = Retest.detect(context, opening_range, breakout)

    assert retest["confirmed"] is True
    # Должна использоваться цена закрытия последней видимой свечи,
    # а НЕ будущей свечи (105.0).
    assert retest["price"] == EXPECTED_VISIBLE_CLOSE


def test_entry_ignores_future_candle():
    market, decision_index = _full_market_with_future_spike()

    context = BacktestContext(index=decision_index, market=market, indicators={}, balance=1000.0)

    opening_range = OpeningRange.calculate(context)
    breakout = Breakout.detect(context, opening_range)
    retest = Retest.detect(context, opening_range, breakout)

    confirmation = {"confirmed": True, "reason": "test"}

    entry = Entry.calculate(context, opening_range, breakout, confirmation)

    assert entry is not None
    # КРИТИЧНО: цена входа обязана быть ценой закрытия последней ВИДИМОЙ
    # свечи, а не будущей свечи (105.0), которая на момент принятия
    # решения ещё не существовала.
    assert entry["entry"] == EXPECTED_VISIBLE_CLOSE
    assert entry["entry"] != FUTURE_SPIKE_CLOSE
