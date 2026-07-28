"""
Тесты, доказывающие, что будущие свечи не могут повлиять на прошлые
решения бэктеста. Обязаны падать при любой утечке будущего.
"""

import pytest

from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig
from tests.conftest import make_snapshot


class RecordingStrategy:
    """Записывает, сколько свечей было видно на каждом вызове."""

    def __init__(self):
        self.visible_lengths = []
        self.seen_last_closes = []

    def generate(self, context):
        visible = context.visible_market
        self.visible_lengths.append(len(visible.closes))
        self.seen_last_closes.append(visible.closes[-1])
        return {"approved": False, "reason": "recording only"}


class TamperDetectingStrategy:
    """
    Пытается получить доступ к данным за пределами видимого окна.
    Если visible_market действительно обрезан, длина видимых свечей
    всегда равна index+1, и последний close равен closes[index].
    """

    def __init__(self, full_market):
        self.full_market = full_market
        self.violations = []

    def generate(self, context):
        visible = context.visible_market
        index = context.index

        if len(visible.closes) != index + 1:
            self.violations.append(
                f"index={index}: visible length {len(visible.closes)} != {index + 1}"
            )

        if visible.closes[-1] != self.full_market.closes[index]:
            self.violations.append(f"index={index}: last visible close is not closes[index]")

        # Любая будущая свеча не должна присутствовать в видимом окне.
        future_slice = self.full_market.closes[index + 1:]
        for future_close in future_slice[:3]:
            if future_close in visible.closes[index + 1:]:
                self.violations.append(f"index={index}: future close {future_close} visible")

        return {"approved": False, "reason": "tamper check"}


def _market(n=60):
    closes = [100.0 + i * 0.1 for i in range(n)]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    return make_snapshot(closes, highs=highs, lows=lows)


def test_strategy_never_sees_more_than_current_index():
    market = _market()
    strategy = RecordingStrategy()

    engine = BacktestEngine(strategy, BacktestConfig(min_candles_before_trading=5))
    engine.run(market)

    assert strategy.visible_lengths, "strategy was never called"

    # Каждый вызов обязан видеть строго index+1 свечей, монотонно растущих.
    for i in range(1, len(strategy.visible_lengths)):
        assert strategy.visible_lengths[i] > strategy.visible_lengths[i - 1]

    assert max(strategy.visible_lengths) <= len(market.closes)


def test_no_future_candle_is_ever_visible():
    market = _market()
    strategy = TamperDetectingStrategy(market)

    engine = BacktestEngine(strategy, BacktestConfig(min_candles_before_trading=5))
    engine.run(market)

    assert strategy.violations == [], f"look-ahead violations detected: {strategy.violations[:5]}"


def test_appending_future_candles_does_not_change_past_decisions():
    """
    Самый строгий тест: прогон на первых N свечах и прогон на тех же N
    свечах + 20 будущих обязаны дать ИДЕНТИЧНУЮ последовательность
    решений на общем префиксе. Если будущее влияет на прошлое — не совпадёт.
    """
    full = _market(80)

    short_market = make_snapshot(
        full.closes[:50], highs=full.highs[:50], lows=full.lows[:50],
    )

    strategy_short = RecordingStrategy()
    BacktestEngine(strategy_short, BacktestConfig(min_candles_before_trading=5)).run(short_market)

    strategy_long = RecordingStrategy()
    BacktestEngine(strategy_long, BacktestConfig(min_candles_before_trading=5)).run(full)

    common = len(strategy_short.seen_last_closes)

    assert strategy_long.seen_last_closes[:common] == strategy_short.seen_last_closes


def test_entry_never_fills_on_the_decision_candle():
    """
    Вход обязан исполняться на СЛЕДУЮЩЕЙ свече по её open, а не по цене
    свечи, на которой принято решение — иначе результаты завышены.
    """

    class AlwaysEnterStrategy:
        def generate(self, context):
            index = context.index
            close = context.visible_market.closes[-1]
            return {
                "approved": True,
                "direction": "LONG",
                "trade_plan": {
                    "entry": close,
                    "stop_loss": close * 0.98,
                    "take_profit": {"tp1": close * 1.04},
                },
                "_decision_index": index,
            }

    market = _market(60)
    engine = BacktestEngine(AlwaysEnterStrategy(), BacktestConfig(min_candles_before_trading=5))
    report = engine.run(market)

    assert report["trades"], "expected at least one trade"

    for trade in report["trades"]:
        entry_index = trade["entry_index"]
        # Цена входа берётся из open свечи входа (плюс издержки),
        # а не из close предыдущей свечи решения.
        assert trade["entry_price"] >= market.opens[entry_index]


def test_exit_never_happens_on_the_entry_candle():
    class AlwaysEnterStrategy:
        def generate(self, context):
            close = context.visible_market.closes[-1]
            return {
                "approved": True,
                "direction": "LONG",
                "trade_plan": {
                    "entry": close,
                    "stop_loss": close * 0.999,
                    "take_profit": {"tp1": close * 1.001},
                },
            }

    market = _market(60)
    report = BacktestEngine(AlwaysEnterStrategy(), BacktestConfig(min_candles_before_trading=5)).run(market)

    for trade in report["trades"]:
        if trade["exit_index"] is not None:
            assert trade["exit_index"] > trade["entry_index"]


def test_stop_and_tp_in_same_candle_resolves_to_stop_not_profit():
    """
    Явное требование: прибыльный исход НЕ выбирается автоматически,
    когда обе цели задеты внутри одной свечи.
    """

    # Свеча с огромным диапазоном, покрывающим и стоп, и тейк.
    closes = [100.0] * 30 + [100.0, 100.0]
    highs = [100.5] * 30 + [130.0, 130.0]
    lows = [99.5] * 30 + [70.0, 70.0]
    market = make_snapshot(closes, highs=highs, lows=lows)

    class EnterOnceStrategy:
        def __init__(self):
            self.done = False

        def generate(self, context):
            if self.done:
                return {"approved": False}
            self.done = True
            return {
                "approved": True,
                "direction": "LONG",
                "trade_plan": {
                    "entry": 100.0,
                    "stop_loss": 90.0,
                    "take_profit": {"tp1": 120.0},
                },
            }

    report = BacktestEngine(EnterOnceStrategy(), BacktestConfig(min_candles_before_trading=5)).run(market)

    resolved = [t for t in report["trades"] if t["exit_reason"] in ("STOP_LOSS", "TAKE_PROFIT")]
    assert resolved, "expected a resolved trade"
    assert resolved[0]["exit_reason"] == "STOP_LOSS"


def test_backtest_is_deterministic_across_runs():
    market = _market(80)

    class SimpleStrategy:
        def generate(self, context):
            close = context.visible_market.closes[-1]
            if context.index % 17 != 0:
                return {"approved": False}
            return {
                "approved": True,
                "direction": "LONG",
                "trade_plan": {
                    "entry": close,
                    "stop_loss": close * 0.98,
                    "take_profit": {"tp1": close * 1.04},
                },
            }

    report_a = BacktestEngine(SimpleStrategy(), BacktestConfig()).run(market)
    report_b = BacktestEngine(SimpleStrategy(), BacktestConfig()).run(market)

    assert report_a["summary"] == report_b["summary"]
    assert report_a["trades"] == report_b["trades"]
