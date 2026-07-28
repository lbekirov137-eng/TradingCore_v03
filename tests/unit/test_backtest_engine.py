import json
import os

import pytest

from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig
from api.backtesting.walk_forward import train_test_split, walk_forward, sensitivity_analysis
from tests.conftest import make_snapshot


def _trending_market(n=120, start=100.0, step=0.15):
    closes = [start + i * step for i in range(n)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    return make_snapshot(closes, highs=highs, lows=lows)


def _flat_market(n=120, price=100.0):
    closes = [price + (0.05 if i % 2 == 0 else -0.05) for i in range(n)]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    return make_snapshot(closes, highs=highs, lows=lows)


class NoTradeStrategy:
    def generate(self, context):
        return {"approved": False, "reason": "never trades"}


class SingleTradeStrategy:
    """Enters once at a fixed index with an explicit, hand-computable plan."""

    def __init__(self, at_index=30, entry=None, stop_pct=0.98, tp_pct=1.04):
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
            "approved": True,
            "direction": "LONG",
            "trade_plan": {
                "entry": close,
                "stop_loss": close * self.stop_pct,
                "take_profit": {"tp1": close * self.tp_pct},
            },
        }


class CrashingStrategy:
    def generate(self, context):
        raise ValueError("strategy blew up")


class TestBasicExecution:

    def test_no_trade_strategy_produces_empty_report(self):
        report = BacktestEngine(NoTradeStrategy(), BacktestConfig()).run(_trending_market())

        assert report["summary"]["total_trades"] == 0
        assert report["summary"]["net_pnl"] == 0
        assert report["summary"]["final_balance"] == 1000.0

    def test_single_trade_in_uptrend_hits_take_profit(self):
        report = BacktestEngine(SingleTradeStrategy(), BacktestConfig()).run(_trending_market(n=200))

        trades = [t for t in report["trades"] if t["exit_reason"] in ("STOP_LOSS", "TAKE_PROFIT")]
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "TAKE_PROFIT"
        assert trades[0]["net_pnl"] > 0

    def test_crashing_strategy_does_not_break_backtest(self):
        report = BacktestEngine(CrashingStrategy(), BacktestConfig()).run(_trending_market())
        assert report["summary"]["total_trades"] == 0

    def test_unresolved_trade_at_end_of_data_is_flagged_not_closed_favourably(self):
        # TP so far away it can never be hit within the data.
        strategy = SingleTradeStrategy(at_index=110, tp_pct=10.0, stop_pct=0.01)
        report = BacktestEngine(strategy, BacktestConfig()).run(_trending_market(n=120))

        unresolved = [t for t in report["trades"] if t["result"] == "UNRESOLVED"]
        assert len(unresolved) == 1
        assert unresolved[0]["exit_reason"] == "END_OF_DATA"
        assert report["summary"]["unresolved_trades"] == 1
        # An unresolved trade must not contribute phantom profit.
        assert report["summary"]["net_pnl"] == 0


class TestCostsApplied:

    def test_fees_are_charged_and_reduce_pnl(self):
        no_fee = BacktestConfig(fee_rate=0.0, slippage_bps=0.0, spread_bps=0.0)
        with_fee = BacktestConfig(fee_rate=0.01, slippage_bps=0.0, spread_bps=0.0)

        market = _trending_market(n=200)

        r1 = BacktestEngine(SingleTradeStrategy(), no_fee).run(market)
        r2 = BacktestEngine(SingleTradeStrategy(), with_fee).run(market)

        assert r2["summary"]["total_fees"] > r1["summary"]["total_fees"]
        assert r2["summary"]["net_pnl"] < r1["summary"]["net_pnl"]

    def test_slippage_worsens_entry_price(self):
        market = _trending_market(n=200)

        no_slip = BacktestEngine(SingleTradeStrategy(), BacktestConfig(slippage_bps=0.0, spread_bps=0.0)).run(market)
        with_slip = BacktestEngine(SingleTradeStrategy(), BacktestConfig(slippage_bps=50.0, spread_bps=0.0)).run(market)

        assert with_slip["trades"][0]["entry_price"] > no_slip["trades"][0]["entry_price"]


class TestMetrics:

    def test_metrics_are_internally_consistent(self):
        report = BacktestEngine(SingleTradeStrategy(), BacktestConfig()).run(_trending_market(n=200))
        summary = report["summary"]

        assert summary["wins"] + summary["losses"] <= summary["total_trades"]

        if summary["total_trades"] > 0:
            recomputed_win_rate = summary["wins"] / summary["total_trades"] * 100
            assert abs(summary["win_rate_percent"] - recomputed_win_rate) < 0.01

    def test_net_pnl_equals_sum_of_trade_net_pnl(self):
        report = BacktestEngine(SingleTradeStrategy(), BacktestConfig()).run(_trending_market(n=200))

        resolved = [t for t in report["trades"] if t["result"] in ("WIN", "LOSS", "BREAKEVEN")]
        expected = round(sum(t["net_pnl"] for t in resolved), 8)

        assert report["summary"]["net_pnl"] == pytest.approx(expected)

    def test_max_drawdown_is_never_negative(self):
        report = BacktestEngine(SingleTradeStrategy(), BacktestConfig()).run(_trending_market(n=200))
        assert report["summary"]["max_drawdown_absolute"] >= 0


class TestExport:

    def test_json_export_is_valid_and_reloadable(self, tmp_path):
        report = BacktestEngine(SingleTradeStrategy(), BacktestConfig()).run(_trending_market(n=200))
        path = str(tmp_path / "report.json")

        BacktestEngine.export_json(report, path)

        with open(path, encoding="utf-8") as f:
            reloaded = json.load(f)

        assert reloaded["summary"] == report["summary"]

    def test_csv_export_has_header_and_rows(self, tmp_path):
        report = BacktestEngine(SingleTradeStrategy(), BacktestConfig()).run(_trending_market(n=200))
        path = str(tmp_path / "trades.csv")

        BacktestEngine.export_trades_csv(report, path)

        content = open(path, encoding="utf-8").read().strip().splitlines()
        assert content[0].startswith("entry_index,")
        assert len(content) == len(report["trades"]) + 1


class TestWalkForwardAndSplits:

    def test_train_test_split_partitions_without_overlap(self):
        market = _trending_market(n=100)
        splits = train_test_split(market, train_ratio=0.6, validation_ratio=0.2)

        assert len(splits["train"].timestamps) == 60
        assert len(splits["validation"].timestamps) == 20
        assert len(splits["test"].timestamps) == 20

        # No timestamp appears in more than one split.
        all_ts = (splits["train"].timestamps + splits["validation"].timestamps + splits["test"].timestamps)
        assert len(all_ts) == len(set(all_ts)) == 100

    def test_walk_forward_produces_windows(self):
        market = _trending_market(n=300)
        result = walk_forward(SingleTradeStrategy(), market, BacktestConfig(), window_size=100, step=50)

        assert result["window_count"] >= 2
        assert "consistency_percent" in result

    def test_sensitivity_analysis_reports_verdict(self):
        market = _trending_market(n=200)
        result = sensitivity_analysis(SingleTradeStrategy(), market, BacktestConfig())

        assert result["verdict"] in ("ROBUST", "FRAGILE", "INSUFFICIENT_DATA")
        assert "baseline" in result["scenarios"]
        assert "all_costs_x2" in result["scenarios"]

    def test_no_trade_strategy_is_insufficient_data_not_robust(self):
        market = _flat_market(n=200)
        result = sensitivity_analysis(NoTradeStrategy(), market, BacktestConfig())
        assert result["verdict"] == "INSUFFICIENT_DATA"
