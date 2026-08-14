#!/usr/bin/env python3
"""
TradingCore Strategy Lab — long-history deep dive.

RESEARCH ONLY. No order client, no API keys, no champion mutation, no LIVE path.

Purpose:
- discard the clearly failed legacy 5m generation from the broad screen;
- run only structurally improved V2 candidates plus precision-aware V4;
- use long 1H/4H history across the supported symbol universe;
- preserve the existing backtest, cost, walk-forward/OOS and promotion gates;
- accelerate 4H context lookup without changing strategy decisions.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
import types
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strategy_lab_orchestrator import (
    DEFAULT_SYMBOLS,
    PRECISION_SENSITIVE,
    REJECT,
    RESEARCH_SHORTLIST,
    PROMOTION_PASSED,
    _evaluate_result,
    _rank_key,
    download_dataset,
    run_context_backtest,
)

RESULT_SCHEMA = "TRADINGCORE_STRATEGY_LAB_DEEP_V1"
CONTEXT_MS = 4 * 60 * 60 * 1000


def _ema_prefix(values: list[float], period: int) -> list[float | None]:
    """Exact prefix equivalent of contracts.ema(), computed once in O(n)."""
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2.0 / (period + 1.0)

    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current

    return result


def install_cached_context(strategy: Any, context_candles: list[Any]) -> None:
    """
    Replace only the expensive context lookup with an exactly equivalent cache.

    Original V2 logic repeatedly scans all 4H candles and recalculates EMA20/50
    for every 1H bar. Here the same closed-4H boundary and the same EMA recurrence
    are precomputed once. Trading rules and thresholds are untouched.
    """
    closes = [float(candle.close) for candle in context_candles]
    fast_prefix = _ema_prefix(closes, 20)
    slow_prefix = _ema_prefix(closes, 50)
    available_at = [int(candle.open_time_ms) + CONTEXT_MS for candle in context_candles]

    def cached_context_is_bullish(self, window, _context_candles):
        deadline = int(window.current.open_time_ms)
        index = bisect.bisect_right(available_at, deadline) - 1

        if index < 0:
            return False, {"context": "UNAVAILABLE"}

        fast = fast_prefix[index]
        slow = slow_prefix[index]

        if fast is None or slow is None:
            return False, {"context": "INSUFFICIENT_CONTEXT_HISTORY"}

        latest = context_candles[index]
        return fast > slow, {
            "context_close": round(float(latest.close), 2),
            "context_fast_ema": round(float(fast), 2),
            "context_slow_ema": round(float(slow), 2),
            "context_open_time_ms": latest.open_time_ms,
        }

    strategy.context_is_bullish = types.MethodType(cached_context_is_bullish, strategy)


def analyze_symbol(job: dict[str, Any]) -> list[dict[str, Any]]:
    from api.strategy_engine.dataset_quality import (
        audit_dataset,
        load_research_candles,
        quality_verdict,
    )
    from api.strategy_engine.strategies.v2_structural import (
        LondonSessionBreakoutRetestV2,
        SessionVwapTrendPullbackV2,
        TrendPullbackEmaStructureV2,
    )
    from api.strategy_engine.strategies.v4_precision import (
        SessionVwapRangeLowVolPrecision,
    )

    symbol = job["symbol"]
    paths = {key: Path(value) for key, value in job["paths"].items()}

    audits: dict[str, dict[str, Any]] = {}
    candles: dict[str, list[Any]] = {}

    for interval in ("1h", "4h"):
        path = paths.get(interval)
        if path is None:
            return [{
                "candidate": f"DATA:{symbol}",
                "strategy_key": "DATA_QUALITY",
                "symbol": symbol,
                "status": REJECT,
                "error": f"missing {interval} dataset",
                "real_orders_enabled": False,
            }]

        report = audit_dataset(path)
        verdict = quality_verdict(report)
        audits[interval] = {"report": report, "verdict": verdict}

        if not verdict.get("usable"):
            return [{
                "candidate": f"DATA:{symbol}",
                "strategy_key": "DATA_QUALITY",
                "symbol": symbol,
                "status": REJECT,
                "error": f"unusable {interval}: {verdict.get('reasons')}",
                "dataset_audits": audits,
                "real_orders_enabled": False,
            }]

        candles[interval] = load_research_candles(path)

    hashes = [
        audits["1h"]["report"]["file_sha256"],
        audits["4h"]["report"]["file_sha256"],
    ]

    strategy_factories: list[Any] = []

    # V2 still rounds price levels to 2 decimals and is invalid for these symbols.
    if symbol not in PRECISION_SENSITIVE:
        strategy_factories.extend([
            SessionVwapTrendPullbackV2,
            LondonSessionBreakoutRetestV2,
            TrendPullbackEmaStructureV2,
        ])

    # V4 is precision-aware and valid for every supported symbol fixture.
    strategy_factories.append(lambda: SessionVwapRangeLowVolPrecision(symbol=symbol))

    results: list[dict[str, Any]] = []

    for factory in strategy_factories:
        strategy = factory()
        install_cached_context(strategy, candles["4h"])

        backtest = run_context_backtest(
            strategy,
            candles["1h"],
            candles["4h"],
            max_bars_in_trade=24,
        )

        item = _evaluate_result(
            symbol=symbol,
            timeframe="1h+4h",
            strategy=strategy,
            backtest=backtest,
            dataset_hashes=hashes,
        )
        item["deep_dive"] = True
        item["history_rows_1h"] = len(candles["1h"])
        item["history_rows_4h"] = len(candles["4h"])
        item["dataset_first_utc"] = audits["1h"]["report"].get("first_utc")
        item["dataset_last_utc"] = audits["1h"]["report"].get("last_utc")
        item["dataset_coverage_years"] = audits["1h"]["report"].get("coverage_years")
        results.append(item)

    return results


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 96)
    lines.append("TRADINGCORE STRATEGY LAB — LONG-HISTORY DEEP DIVE")
    lines.append("=" * 96)
    lines.append(f"Generated UTC: {report['generated_at_utc']}")
    lines.append(f"Requested days: {report['days']}")
    lines.append(f"Symbols: {', '.join(report['symbols'])}")
    lines.append("Scope: V2 structural + V4 precision-aware only; legacy 5m excluded")
    lines.append("MODE: RESEARCH ONLY | REAL ORDERS: IMPOSSIBLE FROM THIS SCRIPT")
    lines.append("")

    counts = report["counts"]
    lines.append(
        f"Candidates={counts['total']}  promotion_passed={counts['promotion_passed']}  "
        f"research_shortlist={counts['research_shortlist']}  "
        f"rejected_or_more_data={counts['rejected_or_more_data']}"
    )
    lines.append("")

    for index, item in enumerate(report["ranking"], start=1):
        validation = item.get("validation") or {}
        stats = item.get("stats") or {}
        gates = item.get("promotion_gates") or {}
        lines.append(f"{index:02d}. {item.get('candidate')}  [{item.get('status')}]")
        if item.get("error"):
            lines.append(f"    ERROR={item['error']}")
            lines.append("")
            continue
        lines.append(
            "    "
            f"coverage={item.get('dataset_coverage_years')}y "
            f"trades={stats.get('closed_trades')} OOS={validation.get('oos_trades')} "
            f"PF={validation.get('oos_profit_factor')} expR={validation.get('oos_expectancy_r')} "
            f"net={validation.get('oos_net_pnl')} DD_R={validation.get('oos_max_drawdown_r')} "
            f"robust={validation.get('robustness_ratio')} WF={validation.get('walk_forward_passed')}"
        )
        failed = gates.get("failed_gates") or []
        if failed:
            lines.append(f"    failed_gates={','.join(failed)}")
        lines.append("")

    lines.append("-" * 96)
    lines.append("Promotion PASS is still research evidence, not permission for LIVE.")
    lines.append("Current 24/7 PAPER champion is not modified by this script.")
    lines.append("-" * 96)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="TradingCore long-history deep Strategy Lab")
    parser.add_argument("--days", type=int, default=3000)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--download-workers", type=int, default=3)
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--output", default="strategy_lab_deep_results")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if args.days < 365:
        raise SystemExit("--days must be >= 365 for deep dive")

    from config.startup_safety import assert_safe_startup
    safety = assert_safe_startup()

    symbols = [str(value).strip().upper() for value in args.symbols if str(value).strip()]
    symbols = [symbol for symbol in symbols if symbol in DEFAULT_SYMBOLS]
    if not symbols:
        raise SystemExit("No supported symbols selected")

    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    dataset_dir = output_dir / "datasets"
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 96)
    print("TRADINGCORE STRATEGY LAB — LONG-HISTORY DEEP DIVE")
    print("=" * 96)
    print("Safety:", safety)
    print("Symbols:", ", ".join(symbols))
    print("Requested days:", args.days)
    print("Scope: 1H+4H V2/V4 only; no legacy 5m")
    print("Real orders: DISABLED / no order code")
    print("=" * 96, flush=True)

    requests_to_make: list[tuple[str, str, Path]] = []
    for symbol in symbols:
        for interval in ("1h", "4h"):
            requests_to_make.append((
                symbol,
                interval,
                dataset_dir / f"{symbol}_{interval}_{args.days}d.json",
            ))

    downloads: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.download_workers)) as pool:
        future_map = {
            pool.submit(
                download_dataset, symbol, interval, args.days, path, args.refresh
            ): (symbol, interval, path)
            for symbol, interval, path in requests_to_make
        }
        for future in as_completed(future_map):
            symbol, interval, path = future_map[future]
            try:
                result = future.result()
                downloads.append(result)
                print(
                    f"[DATA] {symbol} {interval}: rows={result['rows']} cached={result['cached']}",
                    flush=True,
                )
            except Exception as error:
                downloads.append({
                    "symbol": symbol,
                    "interval": interval,
                    "path": str(path),
                    "error": f"{type(error).__name__}: {error}",
                })
                print(f"[DATA] {symbol} {interval}: FAILED {error}", file=sys.stderr, flush=True)

    jobs: list[dict[str, Any]] = []
    for symbol in symbols:
        paths: dict[str, str] = {}
        for item in downloads:
            if item.get("symbol") == symbol and not item.get("error"):
                paths[item["interval"]] = item["path"]
        jobs.append({"symbol": symbol, "paths": paths})

    all_results: list[dict[str, Any]] = []
    worker_count = min(max(1, args.workers), len(jobs))

    with ProcessPoolExecutor(max_workers=worker_count) as pool:
        future_map = {pool.submit(analyze_symbol, job): job["symbol"] for job in jobs}
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                results = future.result()
                all_results.extend(results)
                print(f"[DEEP ANALYST] {symbol}: {len(results)} result(s)", flush=True)
            except Exception as error:
                all_results.append({
                    "candidate": f"WORKER:{symbol}",
                    "strategy_key": "WORKER_ERROR",
                    "symbol": symbol,
                    "status": REJECT,
                    "error": f"{type(error).__name__}: {error}",
                    "real_orders_enabled": False,
                })
                print(f"[DEEP ANALYST] {symbol}: FAILED {error}", file=sys.stderr, flush=True)

    ranking = sorted(all_results, key=_rank_key, reverse=True)
    report = {
        "schema": RESULT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "RESEARCH_ONLY",
        "real_orders_enabled": False,
        "paper_champion_modified": False,
        "live_code_modified": False,
        "days": args.days,
        "symbols": symbols,
        "worker_count": worker_count,
        "downloads": downloads,
        "counts": {
            "total": len(ranking),
            "promotion_passed": sum(1 for item in ranking if item.get("status") == PROMOTION_PASSED),
            "research_shortlist": sum(1 for item in ranking if item.get("status") == RESEARCH_SHORTLIST),
            "rejected_or_more_data": sum(1 for item in ranking if item.get("status") == REJECT),
        },
        "ranking": ranking,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"deep_strategy_lab_{stamp}.json"
    text_path = output_dir / f"deep_strategy_lab_{stamp}.txt"
    latest_path = output_dir / "LATEST_DEEP.txt"

    text = render(report)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")

    print("")
    print(text)
    print("")
    print("JSON:", json_path)
    print("TEXT:", text_path)
    print("LATEST:", latest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
