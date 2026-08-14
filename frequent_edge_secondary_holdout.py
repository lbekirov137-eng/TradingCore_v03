#!/usr/bin/env python3
"""
TradingCore frequent-edge secondary untouched holdout validation.

RESEARCH ONLY. No exchange order client, no API keys, no champion mutation,
no LIVE path.

Why this exists:
- the first global development winner (SOLUSDT 30m VWAP overshoot reclaim)
  was frozen and tested on its untouched final 30% holdout;
- that SOL holdout failed promotion gates;
- frequent_edge_research.py opened ONLY the selected SOL holdout;
- BCHUSDT 30m VWAP overshoot reclaim was the second-ranked DEVELOPMENT
  candidate and its final 30% holdout has not been used by that script.

This file therefore performs one pre-declared secondary confirmatory test:
BCHUSDT / 30m / MR_VWAP_OVERSHOOT_RECLAIM, unchanged parameters.

A pass here is NOT LIVE permission and does not erase the failed SOL result.
Any pass still requires a new forward PAPER confirmation after this freeze.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frequent_edge_research as fer

from api.strategy_engine.backtest_runner import run_backtest
from api.strategy_engine.dataset_quality import (
    audit_dataset,
    load_research_candles,
    quality_verdict,
)
from api.strategy_engine.strategies.contracts import StrategyConfig
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import build_stats
from api.strategy_supervisor.validation import (
    build_walk_forward_windows,
    robustness_ratio,
)
from config.startup_safety import assert_safe_startup


SCHEMA = "TRADINGCORE_FREQUENT_EDGE_SECONDARY_HOLDOUT_V1"
SYMBOL = "BCHUSDT"
TIMEFRAME = "30m"
STRATEGY_KEY = "MR_VWAP_OVERSHOOT_RECLAIM_30M"
DEV_FRACTION = 0.70
DAYS = 3000
MAX_BARS_IN_TRADE = fer.TIMEFRAMES[TIMEFRAME]["max_bars"]


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--dev-fraction", type=float, default=DEV_FRACTION)
    parser.add_argument("--output", default="frequent_edge_secondary_results")
    args = parser.parse_args()

    if abs(args.dev_fraction - DEV_FRACTION) > 1e-12:
        raise SystemExit("Secondary confirmatory split is frozen at 0.70")
    if args.days != DAYS:
        raise SystemExit("Secondary confirmatory history is frozen at 3000 days")

    safety = assert_safe_startup()
    fer.patch_intervals()

    out = Path(args.output)
    if not out.is_absolute():
        out = Path.cwd() / out
    out.mkdir(parents=True, exist_ok=True)

    # Reuse the already-downloaded full BCH 30m history from the original lab.
    source = Path.cwd() / "frequent_edge_results" / "datasets" / f"{SYMBOL}_{TIMEFRAME}_{DAYS}d.json"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        fer.broad.download_dataset(SYMBOL, TIMEFRAME, DAYS, source, refresh=False)

    audit = audit_dataset(source)
    verdict = quality_verdict(audit)
    if not verdict.get("usable"):
        raise SystemExit(f"Dataset unusable: {verdict.get('reasons')}")

    rows = load_research_candles(source)
    if len(rows) < 500:
        raise SystemExit("Insufficient BCH 30m history")

    split_index = max(1, min(len(rows) - 1, int(len(rows) * DEV_FRACTION)))
    boundary_ms = int(rows[split_index].open_time_ms)
    boundary_utc = _utc(boundary_ms)
    dev_rows = [c for c in rows if int(c.open_time_ms) < boundary_ms]

    # Frozen strategy, unchanged from original frequent-edge research.
    dev_strategy = fer.VwapOvershootReclaim(StrategyConfig(warmup_bars=60))
    dev_strategy.strategy_key = STRATEGY_KEY
    dev_bt = run_backtest(
        dev_strategy,
        dev_rows,
        max_bars_in_trade=MAX_BARS_IN_TRADE,
    )
    dev_stats = build_stats(dev_bt["trades"])
    windows = build_walk_forward_windows(dev_bt["trades"], window_count=4)
    robust = robustness_ratio(windows)
    wf = bool(windows and robust is not None and robust > 0.0)

    # Sanity gate: this must still be the same second development candidate.
    if dev_stats.get("closed_trades") != 40:
        raise SystemExit(
            f"Frozen development fingerprint mismatch: expected 40 trades, got {dev_stats.get('closed_trades')}"
        )

    final_strategy = fer.VwapOvershootReclaim(StrategyConfig(warmup_bars=60))
    final_strategy.strategy_key = STRATEGY_KEY
    fer.suppress_before(final_strategy, boundary_ms)

    final_bt = run_backtest(
        final_strategy,
        rows,
        max_bars_in_trade=MAX_BARS_IN_TRADE,
    )
    final_trades = [
        trade for trade in final_bt["trades"]
        if isinstance(trade.closed_at_utc, str) and trade.closed_at_utc >= boundary_utc
    ]
    final_stats = build_stats(final_trades)

    validation: dict[str, Any] = {
        "strategy_id": f"{STRATEGY_KEY}:{SYMBOL}",
        "sample_id": f"FREQUENT_EDGE_SECONDARY_{SYMBOL}_{TIMEFRAME}_{boundary_ms}",
        "oos_trades": final_stats["closed_trades"],
        "oos_net_pnl": final_stats["net_pnl"],
        "oos_profit_factor": final_stats["profit_factor"],
        "oos_expectancy_r": final_stats["expectancy_r"],
        "oos_max_drawdown_r": final_stats["max_drawdown_r"],
        "oos_win_rate_percent": final_stats["win_rate_percent"],
        "robustness_ratio": robust,
        "walk_forward_passed": wf,
        "look_ahead_leakage": False,
        "safety_violations": [],
        "holdout_start_utc": boundary_utc,
        "note": (
            "Secondary confirmatory holdout. Candidate/timeframe/parameters were frozen from "
            "the original DEVELOPMENT ranking. The original frequent-edge script opened only "
            "SOL's final holdout, not this BCH holdout. Costs/slippage included."
        ),
    }
    gates = promotion_gates(validation)

    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "RESEARCH_ONLY",
        "real_orders_enabled": False,
        "paper_champion_modified": False,
        "safety": safety,
        "candidate": f"{SYMBOL} {TIMEFRAME} {STRATEGY_KEY}",
        "holdout_start_utc": boundary_utc,
        "development": {
            "stats": dev_stats,
            "robustness_ratio": robust,
            "walk_forward_passed": wf,
        },
        "final_untouched_holdout": {
            "stats": final_stats,
            "validation": validation,
            "promotion_gates": gates,
        },
        "dataset_audit": audit,
        "note": (
            "A PASS is research evidence only. Because this is a secondary sequential test, "
            "forward PAPER confirmation is mandatory before any later owner decision on LIVE."
        ),
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"secondary_holdout_{stamp}.json"
    text_path = out / f"secondary_holdout_{stamp}.txt"
    latest_path = out / "LATEST_SECONDARY_HOLDOUT.txt"

    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    lines = [
        "=" * 92,
        "TRADINGCORE FREQUENT EDGE — SECONDARY UNTOUCHED HOLDOUT",
        "=" * 92,
        f"Generated UTC: {report['generated_at_utc']}",
        f"Candidate: {report['candidate']}",
        f"Holdout start: {boundary_utc}",
        "",
        "DEVELOPMENT (already known, used only to verify frozen identity):",
        (
            f"  trades={dev_stats['closed_trades']} PF={dev_stats['profit_factor']} "
            f"expR={dev_stats['expectancy_r']} net={dev_stats['net_pnl']} "
            f"DD_R={dev_stats['max_drawdown_r']} robust={robust} WF={wf}"
        ),
        "",
        "SECONDARY FINAL UNTOUCHED HOLDOUT:",
        (
            f"  trades={final_stats['closed_trades']} PF={final_stats['profit_factor']} "
            f"expR={final_stats['expectancy_r']} net={final_stats['net_pnl']} "
            f"DD_R={final_stats['max_drawdown_r']} win={final_stats['win_rate_percent']}"
        ),
        f"  promotion_passed={gates['passed']}",
        f"  failed_gates={','.join(gates['failed_gates']) if gates['failed_gates'] else 'NONE'}",
        "",
        "RESEARCH ONLY. NO LIVE ORDER PATH. PASS still requires new forward PAPER confirmation.",
    ]

    text = "\n".join(lines)
    text_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")

    print("\n" + text)
    print("\nJSON:", json_path)
    print("TEXT:", text_path)
    print("LATEST:", latest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
