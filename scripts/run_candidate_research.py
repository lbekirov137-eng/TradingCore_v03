"""
Оценивает исследовательские варианты ORB и VWAP ОТДЕЛЬНО друг от друга
и от baseline. Ни один вариант не объединяется с другими и не заменяет
baseline автоматически -- только честный, отдельный отчёт по каждому.

Пример:
    python scripts/run_candidate_research.py --data data/BTCUSDT_5m.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.market_data.market_snapshot import MarketSnapshot
from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig
from api.backtesting.research import (
    min_sample_size_check, benchmark_buy_and_hold, benchmark_no_trade,
)

from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy
from api.strategy_engine.strategies.orb.candidates import ORBWithRangeATRFilter, ORBWithMinRelativeVolume
from api.strategy_engine.strategies.vwap.vwap_strategy import VWAPTrendPullbackStrategy
from api.strategy_engine.strategies.vwap.candidates import (
    VWAPTighterPullback, VWAPWiderPullback, VWAPWithVolumeConfirmation,
)


def load_market(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return MarketSnapshot(
        exchange="binance", symbol=data["symbol"], interval=data["interval"],
        timestamps=data["timestamps"], opens=data["opens"], highs=data["highs"],
        lows=data["lows"], closes=data["closes"], volumes=data["volumes"],
    )


CANDIDATES = {
    "ORB_baseline": ORBStrategy,
    "ORB_range_atr_filter": ORBWithRangeATRFilter(min_ratio=0.3, max_ratio=3.0),
    "ORB_min_relative_volume": ORBWithMinRelativeVolume(min_volume_ratio=1.0),
    "VWAP_baseline": VWAPTrendPullbackStrategy,
    "VWAP_tighter_pullback": VWAPTighterPullback,
    "VWAP_wider_pullback": VWAPWiderPullback,
    "VWAP_volume_confirmation": VWAPWithVolumeConfirmation,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", default="reports/candidate_research.json")
    args = parser.parse_args()

    market = load_market(args.data)
    config = BacktestConfig()

    results = {
        "benchmarks": {
            "no_trade": benchmark_no_trade(),
            "buy_and_hold": benchmark_buy_and_hold(market),
        },
        "candidates": {},
    }

    for name, strategy in CANDIDATES.items():
        report = BacktestEngine(strategy, config).run(market)
        sample_check = min_sample_size_check(report["summary"], min_trades=30)

        results["candidates"][name] = {
            "summary": report["summary"],
            "sample_size": sample_check,
        }

        print(f"{name:28s} trades={report['summary']['total_trades']:3d} "
              f"net_pnl={report['summary']['net_pnl']:>10.4f} "
              f"win_rate={report['summary']['win_rate_percent']:5.1f}% "
              f"pf={report['summary']['profit_factor']} "
              f"sufficient_sample={sample_check['sufficient']}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nSaved: {args.out}")
    print(f"Benchmark buy-and-hold net_pnl: {results['benchmarks']['buy_and_hold']['net_pnl']:.4f}")


if __name__ == "__main__":
    main()
