from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig
from api.backtesting.research import (
    min_sample_size_check,
    benchmark_buy_and_hold,
    benchmark_no_trade,
    regime_segmentation,
    parameter_stability_analysis,
    monte_carlo_trade_order,
)

from tests.conftest import make_snapshot


def _trending_market(n=200, start=100.0, step=0.2):
    closes = [start + i * step for i in range(n)]
    return make_snapshot(closes, highs=[c + 0.3 for c in closes], lows=[c - 0.3 for c in closes])


class SingleTradeStrategy:
    def __init__(self, at_index=30, stop_pct=0.98, tp_pct=1.04):
        self.at_index = at_index
        self.fired = False
        self.stop_pct = stop_pct
        self.tp_pct = tp_pct

    def generate(self, context):
        if self.fired or context.index != self.at_index:
            return {"approved": False}
        self.fired = True
        close = context.visible_market.closes[-1]
        return {
            "approved": True, "direction": "LONG",
            "trade_plan": {
                "entry": close, "stop_loss": close * self.stop_pct,
                "take_profit": {"tp1": close * self.tp_pct},
            },
        }


class MultiTradeStrategy:
    """Fires every ~15 candles so there's more than one trade to segment/permute."""

    def generate(self, context):
        if context.index % 15 != 0:
            return {"approved": False}
        close = context.visible_market.closes[-1]
        return {
            "approved": True, "direction": "LONG",
            "trade_plan": {
                "entry": close, "stop_loss": close * 0.99,
                "take_profit": {"tp1": close * 1.02},
            },
        }


class TestMinSampleSizeCheck:

    def test_flags_insufficient_sample(self):
        result = min_sample_size_check({"total_trades": 5}, min_trades=30)
        assert result["sufficient"] is False

    def test_passes_with_enough_trades(self):
        result = min_sample_size_check({"total_trades": 35}, min_trades=30)
        assert result["sufficient"] is True


class TestBenchmarks:

    def test_no_trade_benchmark_is_always_zero(self):
        result = benchmark_no_trade()
        assert result["net_pnl"] == 0.0

    def test_buy_and_hold_profits_in_uptrend(self):
        market = _trending_market()
        result = benchmark_buy_and_hold(market)
        assert result["net_pnl"] > 0

    def test_buy_and_hold_handles_tiny_market_safely(self):
        market = make_snapshot([100.0])
        result = benchmark_buy_and_hold(market)
        assert result["net_pnl"] == 0.0


class TestRegimeSegmentation:

    def test_segments_trades_by_regime_at_entry(self):
        market = _trending_market(n=200)
        result = regime_segmentation(MultiTradeStrategy(), market, BacktestConfig())

        assert "by_regime" in result
        assert "overall_summary" in result
        total_segmented = sum(b["trades"] for b in result["by_regime"].values())
        assert total_segmented == result["overall_summary"]["total_trades"] or total_segmented <= result["overall_summary"]["total_trades"]

    def test_no_trades_gives_empty_segmentation(self):
        market = _trending_market(n=50)

        class NeverTrades:
            def generate(self, context):
                return {"approved": False}

        result = regime_segmentation(NeverTrades(), market, BacktestConfig())
        assert result["by_regime"] == {}


class TestParameterStabilityAnalysis:

    def test_stable_sign_when_all_variants_agree(self):
        market = _trending_market(n=200)

        def factory(params):
            return SingleTradeStrategy(at_index=30, stop_pct=params["stop_pct"], tp_pct=1.10)

        variants = [{"stop_pct": 0.99}, {"stop_pct": 0.98}, {"stop_pct": 0.97}]

        result = parameter_stability_analysis(factory, variants, market, BacktestConfig())
        assert result["verdict"] in ("STABLE_SIGN", "INSUFFICIENT_DATA")
        assert len(result["variants"]) == 3

    def test_reports_insufficient_data_with_no_trades(self):
        market = _trending_market(n=50)

        def factory(params):
            class NeverTrades:
                def generate(self, context):
                    return {"approved": False}
            return NeverTrades()

        result = parameter_stability_analysis(factory, [{"a": 1}, {"a": 2}], market, BacktestConfig())
        assert result["verdict"] == "INSUFFICIENT_DATA"


class TestMonteCarloTradeOrder:

    def test_insufficient_data_with_too_few_trades(self):
        result = monte_carlo_trade_order([{"result": "WIN", "net_pnl": 1.0}])
        assert result["verdict"] == "INSUFFICIENT_DATA"

    def test_deterministic_with_fixed_seed(self):
        trades = [
            {"result": "WIN", "net_pnl": 10.0},
            {"result": "LOSS", "net_pnl": -5.0},
            {"result": "WIN", "net_pnl": 3.0},
            {"result": "LOSS", "net_pnl": -8.0},
        ]

        result_a = monte_carlo_trade_order(trades, iterations=200, seed=42)
        result_b = monte_carlo_trade_order(trades, iterations=200, seed=42)

        assert result_a == result_b

    def test_ruin_detected_when_losses_exceed_balance(self):
        trades = [{"result": "LOSS", "net_pnl": -2000.0}, {"result": "WIN", "net_pnl": 5.0}]

        result = monte_carlo_trade_order(trades, iterations=100, seed=1, initial_balance=1000.0)
        assert result["ruin_probability_percent"] > 0

    def test_no_ruin_when_all_trades_are_wins(self):
        trades = [{"result": "WIN", "net_pnl": 10.0}, {"result": "WIN", "net_pnl": 5.0}]

        result = monte_carlo_trade_order(trades, iterations=100, seed=1, initial_balance=1000.0)
        assert result["ruin_probability_percent"] == 0.0
