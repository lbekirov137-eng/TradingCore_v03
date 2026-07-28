"""
Прогон бэктеста с train/validation/out-of-sample split, walk-forward и
стресс-тестом устойчивости. Экспортирует JSON и CSV.

Пример:
    python scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy orb
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.market_data.market_snapshot import MarketSnapshot
from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig
from api.backtesting.walk_forward import train_test_split, walk_forward, sensitivity_analysis
from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy
from api.strategy_engine.strategies.vwap.vwap_strategy import VWAPTrendPullbackStrategy


STRATEGIES = {
    "orb": ORBStrategy,
    "vwap": VWAPTrendPullbackStrategy,
}


def load_market(path: str) -> MarketSnapshot:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return MarketSnapshot(
        exchange="binance", symbol=data["symbol"], interval=data["interval"],
        timestamps=data["timestamps"], opens=data["opens"], highs=data["highs"],
        lows=data["lows"], closes=data["closes"], volumes=data["volumes"],
    )


def _fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--strategy", default="orb", choices=sorted(STRATEGIES))
    parser.add_argument("--outdir", default="reports")
    args = parser.parse_args()

    market = load_market(args.data)
    strategy = STRATEGIES[args.strategy]

    os.makedirs(args.outdir, exist_ok=True)

    print(f"Strategy: {args.strategy}")
    print(f"Candles:  {len(market.timestamps)}")
    print(f"Period:   {_fmt_ts(market.timestamps[0])} -> {_fmt_ts(market.timestamps[-1])}")
    print()

    config = BacktestConfig()

    full = BacktestEngine(strategy, config).run(market)
    print("=== FULL PERIOD ===")
    print(json.dumps(full["summary"], indent=2))
    print()

    splits = train_test_split(market)
    split_results = {}

    for name, segment in splits.items():
        report = BacktestEngine(strategy, config).run(segment)
        split_results[name] = report["summary"]
        print(f"=== {name.upper()} ({len(segment.timestamps)} candles) ===")
        print(json.dumps(report["summary"], indent=2))
        print()

    wf = walk_forward(strategy, market, config)
    print("=== WALK-FORWARD ===")
    print(json.dumps({k: v for k, v in wf.items() if k != "windows"}, indent=2))
    print()

    sens = sensitivity_analysis(strategy, market, config)
    print("=== SENSITIVITY / STRESS ===")
    for name, summary in sens["scenarios"].items():
        print(f"  {name:20s} trades={summary['total_trades']:3d} "
              f"net_pnl={summary['net_pnl']:>10.4f} win_rate={summary['win_rate_percent']:.1f}%")
    print(f"  VERDICT: {sens['verdict']}")
    print()

    bundle = {
        "strategy": args.strategy,
        "data_file": args.data,
        "candles": len(market.timestamps),
        "period_start": _fmt_ts(market.timestamps[0]),
        "period_end": _fmt_ts(market.timestamps[-1]),
        "full_period": full["summary"],
        "splits": split_results,
        "walk_forward": {k: v for k, v in wf.items() if k != "windows"},
        "sensitivity": sens,
    }

    json_path = os.path.join(args.outdir, f"backtest_{args.strategy}.json")
    csv_path = os.path.join(args.outdir, f"trades_{args.strategy}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)

    BacktestEngine.export_trades_csv(full, csv_path)

    print(f"Saved: {json_path}")
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
