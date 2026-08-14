#!/usr/bin/env python3
"""
TradingCore Strategy Research Team — timeframe discovery with strict holdout.

RESEARCH ONLY. No order client, no API keys, no champion mutation, no LIVE path.

Method:
1) Freeze one idea: precision-aware SESSION_VWAP_RANGE_LOW_VOL_PX.
2) Compare only execution timeframe (30m / 1h / 2h) on the EARLY 70% of history.
3) Choose exactly one timeframe using development data only.
4) Open the untouched final 30% only after the choice is frozen.
5) Apply the existing promotion gates to final-holdout metrics plus development
   walk-forward robustness.

This avoids choosing a timeframe after seeing the final test period.
"""
from __future__ import annotations

import argparse
import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import strategy_lab_orchestrator as broad
from strategy_lab_deep_dive import install_cached_context

TIMEFRAMES = {
    "30m": {"ms": 1_800_000, "max_bars": 48},
    "1h":  {"ms": 3_600_000, "max_bars": 24},
    "2h":  {"ms": 7_200_000, "max_bars": 12},
}
CONTEXT = "4h"
SCHEMA = "TRADINGCORE_TIMEFRAME_RESEARCH_V1"


def patch_intervals() -> None:
    broad.INTERVAL_MS.update({
        "30m": TIMEFRAMES["30m"]["ms"],
        "2h": TIMEFRAMES["2h"]["ms"],
    })
    from api.strategy_engine import dataset_quality
    dataset_quality.INTERVAL_MS.update({
        "30m": TIMEFRAMES["30m"]["ms"],
        "2h": TIMEFRAMES["2h"]["ms"],
    })


def score(stats: dict[str, Any], robustness: float | None) -> tuple:
    trades = stats.get("closed_trades") or 0
    pf = stats.get("profit_factor")
    exp = stats.get("expectancy_r")
    pnl = stats.get("net_pnl")
    dd = stats.get("max_drawdown_r")

    def n(value, fallback=-1e9):
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else fallback

    # Require useful sample size first; then reward stable, positive economics.
    adequate = 1 if trades >= 30 else 0
    positive = 1 if n(pnl) > 0 and n(exp) > 0 and n(pf) >= 1.0 else 0
    return (
        adequate,
        positive,
        n(robustness),
        n(pf),
        n(exp),
        n(pnl),
        -n(dd, fallback=1e9),
        trades,
    )


def make_strategy(symbol: str, timeframe: str):
    from api.strategy_engine.strategies.v4_precision import (
        PrecisionConfig,
        SessionVwapRangeLowVolPrecision,
    )
    config = PrecisionConfig(execution_timeframe=timeframe, context_timeframe="4H")
    strategy = SessionVwapRangeLowVolPrecision(config=config, symbol=symbol)
    # Unique research identity. Trading logic is unchanged; timeframe is the only variant.
    strategy.strategy_key = f"SESSION_VWAP_RANGE_LOW_VOL_PX_{timeframe.upper()}"
    strategy.version = "5.0.0-research"
    return strategy


def suppress_entries_before(strategy, boundary_ms: int) -> None:
    original = strategy.evaluate_with_context

    def gated(self, candles, index, context_candles):
        if int(candles[index].open_time_ms) < boundary_ms:
            return self.no_trade("PRE_FINAL_HOLDOUT")
        return original(candles, index, context_candles)

    strategy.evaluate_with_context = types.MethodType(gated, strategy)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3000)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--dev-fraction", type=float, default=0.70)
    parser.add_argument("--output", default="strategy_research_results")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if not 0.55 <= args.dev_fraction <= 0.80:
        raise SystemExit("--dev-fraction must be between 0.55 and 0.80")

    from config.startup_safety import assert_safe_startup
    safety = assert_safe_startup()
    patch_intervals()

    symbol = args.symbol.strip().upper()
    if symbol not in broad.DEFAULT_SYMBOLS:
        raise SystemExit(f"Unsupported symbol: {symbol}")

    out = Path(args.output)
    if not out.is_absolute():
        out = Path.cwd() / out
    data_dir = out / "datasets"
    out.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    intervals = [*TIMEFRAMES.keys(), CONTEXT]
    paths: dict[str, Path] = {}
    downloads = []

    print("=" * 92)
    print("TRADINGCORE STRATEGY RESEARCH TEAM — STRICT FINAL HOLDOUT")
    print("=" * 92)
    print("Safety:", safety)
    print("Symbol:", symbol)
    print("Development fraction:", args.dev_fraction)
    print("Variants:", ", ".join(TIMEFRAMES))
    print("REAL ORDERS: DISABLED / NO ORDER CODE")
    print("=" * 92, flush=True)

    for interval in intervals:
        path = data_dir / f"{symbol}_{interval}_{args.days}d.json"
        result = broad.download_dataset(symbol, interval, args.days, path, args.refresh)
        downloads.append(result)
        paths[interval] = path
        print(f"[DATA] {interval}: rows={result['rows']} cached={result['cached']}", flush=True)

    from api.strategy_engine.dataset_quality import audit_dataset, load_research_candles, quality_verdict
    from api.strategy_supervisor.stats import build_stats
    from api.strategy_supervisor.validation import build_walk_forward_windows, robustness_ratio
    from api.strategy_supervisor.gates import promotion_gates

    candles: dict[str, list[Any]] = {}
    audits = {}
    for interval, path in paths.items():
        report = audit_dataset(path)
        verdict = quality_verdict(report)
        audits[interval] = {"report": report, "verdict": verdict}
        if not verdict.get("usable"):
            raise SystemExit(f"Dataset {interval} unusable: {verdict.get('reasons')}")
        candles[interval] = load_research_candles(path)

    # Common boundary from 1h history; variants use timestamp comparison.
    base = candles["1h"]
    split_index = max(1, min(len(base) - 1, int(len(base) * args.dev_fraction)))
    boundary_ms = int(base[split_index].open_time_ms)
    boundary_utc = datetime.fromtimestamp(boundary_ms / 1000, tz=timezone.utc).isoformat()

    context_all = candles[CONTEXT]
    development_results = []

    for timeframe, meta in TIMEFRAMES.items():
        exec_all = candles[timeframe]
        exec_dev = [c for c in exec_all if int(c.open_time_ms) < boundary_ms]
        context_dev = [c for c in context_all if int(c.open_time_ms) < boundary_ms]

        strategy = make_strategy(symbol, timeframe)
        install_cached_context(strategy, context_dev)
        bt = broad.run_context_backtest(
            strategy, exec_dev, context_dev, max_bars_in_trade=meta["max_bars"]
        )
        stats = build_stats(bt["trades"])
        windows = build_walk_forward_windows(bt["trades"], window_count=4)
        robust = robustness_ratio(windows)
        wf = bool(windows and robust is not None and robust > 0.0)

        item = {
            "timeframe": timeframe,
            "strategy_key": strategy.strategy_key,
            "parameter_fingerprint": strategy.config.fingerprint(),
            "development_stats": stats,
            "development_robustness": robust,
            "development_walk_forward_passed": wf,
            "development_windows": windows,
            "signals": bt["signals"],
        }
        development_results.append(item)
        print(
            f"[DEV] {timeframe}: trades={stats['closed_trades']} PF={stats['profit_factor']} "
            f"expR={stats['expectancy_r']} net={stats['net_pnl']} DD_R={stats['max_drawdown_r']} "
            f"robust={robust} WF={wf}",
            flush=True,
        )

    chosen = max(
        development_results,
        key=lambda item: score(item["development_stats"], item["development_robustness"]),
    )
    chosen_tf = chosen["timeframe"]
    print(f"[FROZEN CHOICE] {chosen_tf} — final holdout has not been used for selection", flush=True)

    # Final holdout: keep pre-boundary candles for indicator warm-up, but structurally
    # forbid entries before the boundary. This prevents a pre-holdout trade from leaking
    # into final test results while preserving legitimate historical indicator context.
    final_strategy = make_strategy(symbol, chosen_tf)
    install_cached_context(final_strategy, context_all)
    suppress_entries_before(final_strategy, boundary_ms)

    final_bt = broad.run_context_backtest(
        final_strategy,
        candles[chosen_tf],
        context_all,
        max_bars_in_trade=TIMEFRAMES[chosen_tf]["max_bars"],
    )
    final_trades = [
        trade for trade in final_bt["trades"]
        if isinstance(trade.closed_at_utc, str) and trade.closed_at_utc >= boundary_utc
    ]
    final_stats = build_stats(final_trades)

    validation = {
        "strategy_id": final_strategy.strategy_key,
        "sample_id": f"STRICT_HOLDOUT_{symbol}_{chosen_tf}_{boundary_ms}",
        "oos_trades": final_stats["closed_trades"],
        "oos_net_pnl": final_stats["net_pnl"],
        "oos_profit_factor": final_stats["profit_factor"],
        "oos_expectancy_r": final_stats["expectancy_r"],
        "oos_max_drawdown_r": final_stats["max_drawdown_r"],
        "oos_win_rate_percent": final_stats["win_rate_percent"],
        "robustness_ratio": chosen["development_robustness"],
        "walk_forward_passed": chosen["development_walk_forward_passed"],
        "look_ahead_leakage": False,
        "safety_violations": [],
        "holdout_start_utc": boundary_utc,
        "note": (
            "Timeframe selected only on early development history; final 30% opened after choice. "
            "Parameters were not optimized on final holdout."
        ),
    }
    gates = promotion_gates(validation)

    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "RESEARCH_ONLY",
        "real_orders_enabled": False,
        "paper_champion_modified": False,
        "symbol": symbol,
        "days": args.days,
        "development_fraction": args.dev_fraction,
        "holdout_start_utc": boundary_utc,
        "development_results": development_results,
        "chosen_timeframe": chosen_tf,
        "chosen_strategy_key": final_strategy.strategy_key,
        "final_holdout_stats": final_stats,
        "validation": validation,
        "promotion_gates": gates,
        "downloads": downloads,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"timeframe_research_{stamp}.json"
    text_path = out / f"timeframe_research_{stamp}.txt"
    latest = out / "LATEST_TIMEFRAME_RESEARCH.txt"

    lines = [
        "=" * 92,
        "TRADINGCORE TIMEFRAME RESEARCH — STRICT FINAL HOLDOUT",
        "=" * 92,
        f"Symbol: {symbol}",
        f"Holdout start: {boundary_utc}",
        f"Frozen choice from development only: {chosen_tf}",
        "",
        "DEVELOPMENT:",
    ]
    for item in development_results:
        s = item["development_stats"]
        lines.append(
            f"  {item['timeframe']}: trades={s['closed_trades']} PF={s['profit_factor']} "
            f"expR={s['expectancy_r']} net={s['net_pnl']} DD_R={s['max_drawdown_r']} "
            f"robust={item['development_robustness']} WF={item['development_walk_forward_passed']}"
        )
    lines += [
        "",
        "FINAL UNTOUCHED HOLDOUT:",
        f"  trades={final_stats['closed_trades']} PF={final_stats['profit_factor']} "
        f"expR={final_stats['expectancy_r']} net={final_stats['net_pnl']} "
        f"DD_R={final_stats['max_drawdown_r']} win={final_stats['win_rate_percent']}",
        f"  promotion_passed={gates['passed']}",
        f"  failed_gates={','.join(gates['failed_gates']) if gates['failed_gates'] else 'NONE'}",
        "",
        "RESEARCH ONLY. A pass is not permission for LIVE; live-PAPER confirmation remains required.",
    ]
    text = "\n".join(lines)

    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print("\n" + text, flush=True)
    print(f"\nJSON: {json_path}\nTEXT: {text_path}\nLATEST: {latest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
