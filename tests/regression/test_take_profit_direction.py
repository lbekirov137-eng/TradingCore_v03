"""
CRITICAL confirmed bug, found via a real 6-month backtest (not by code
inspection): TakeProfit.calculate always computed entry + risk*N,
regardless of trade direction. For a SHORT trade this places the
take-profit ABOVE both entry and the stop -- on the wrong side of the
market, unreachable in the profitable direction.

Concretely: a SHORT opened at entry=89234.8, stop=90648.454 (correctly
above entry) got tp1=92062.108 -- even higher than the stop. Price then
fell from ~89k to ~60k over the following four months (a huge win for
that short), but the position never closed: the correct stop was never
touched (price only ever rose to 89490, short of the 90648 stop) and
the bogus tp was unreachable on the downside. The stuck position
silently blocked every subsequent signal for the rest of the backtest,
producing a report of "0 trades" over a 4-month window that actually
contained real conditions the strategy should have traded.
"""

from api.strategy_engine.strategies.orb.take_profit import TakeProfit


def test_long_take_profit_is_above_entry():
    result = TakeProfit.calculate(entry=100.0, stop=98.0, direction="LONG")

    assert result["tp1"] > 100.0
    assert result["tp2"] > result["tp1"]


def test_short_take_profit_is_below_entry():
    """The bug: this used to equal the LONG case regardless of direction."""
    result = TakeProfit.calculate(entry=100.0, stop=102.0, direction="SHORT")

    assert result["tp1"] < 100.0
    assert result["tp2"] < result["tp1"]


def test_short_take_profit_is_on_the_correct_side_of_the_stop():
    """
    For a SHORT, entry < stop (stop protects against price rising).
    The take-profit must be BELOW entry (protects against... no, profits
    from price falling) -- i.e. tp1 must be on the opposite side of entry
    from the stop, not the same side.
    """
    entry, stop = 89234.8, 90648.454
    result = TakeProfit.calculate(entry, stop, direction="SHORT")

    assert result["tp1"] < entry < stop, (
        f"tp1={result['tp1']} must be below entry={entry}, which must be below stop={stop}"
    )


def test_reproduces_the_exact_stuck_short_scenario():
    """
    Reproduces the exact numbers from the 6-month backtest that surfaced
    this bug. Before the fix, tp1 was 92062.1 (above the stop). After the
    fix, tp1 must be a real, reachable downside target.
    """
    entry, stop = 89234.8, 90648.454
    result = TakeProfit.calculate(entry, stop, direction="SHORT")

    risk = stop - entry
    expected_tp1 = entry - risk * 2

    assert result["tp1"] == expected_tp1
    assert result["tp1"] < entry  # reachable by a falling price, unlike the old bug


def test_default_direction_is_long_for_backward_compatibility():
    result = TakeProfit.calculate(entry=100.0, stop=98.0)  # no direction passed
    assert result["tp1"] > 100.0
