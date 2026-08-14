#!/usr/bin/env python3
"""
TradingCore Strategy Lab Orchestrator
=====================================

RESEARCH ONLY. This file does not import any exchange order client, does not
read API keys, does not alter the live PAPER champion and cannot enable LIVE.

Pipeline:
  public Binance closed candles -> dataset quality -> parallel backtests ->
  chronological walk-forward + OOS holdout -> existing promotion gates ->
  ranked research report.

The orchestrator deliberately reuses TradingCore's existing components instead
of inventing a second validation framework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "LTCUSDT", "SOLUSDT",
    "BCHUSDT", "ADAUSDT", "XRPUSDT", "TRXUSDT",
)

# v1/v2 BaseStrategy rounds levels to 2 decimals. The repository explicitly
# invalidated prior ADA/XRP/TRX results because this destroys order geometry.
# Those symbols are therefore evaluated only with precision-aware @4.0.0.
PRECISION_SENSITIVE = {"ADAUSDT", "XRPUSDT", "TRXUSDT"}

BINANCE_BASES = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)

INTERVAL_MS = {
    "5m": 300_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

RESULT_SCHEMA = "TRADINGCORE_STRATEGY_LAB_V1"
RESEARCH_SHORTLIST = "RESEARCH_SHORTLIST_NOT_LIVE"
PROMOTION_PASSED = "PROMOTION_GATES_PASSED_NOT_LIVE"
REJECT = "REJECT_OR_MORE_DATA"


def _request_klines(params: dict[str, Any]) -> list[list[Any]]:
    """Public market-data request with conservative retry/backoff."""
    last_error: Exception | None = None

    for base in BINANCE_BASES:
        delay = 1.0
        for attempt in range(6):
            try:
                response = requests.get(
                    f"{base}/api/v3/klines",
                    params=params,
                    timeout=20,
                )

                if response.status_code == 200:
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise RuntimeError("Binance klines response is not a list")
                    return payload

                if response.status_code in (418, 429):
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait = max(delay, float(retry_after)) if retry_after else delay
                    except ValueError:
                        wait = delay
                    time.sleep(min(wait, 30.0))
                    delay = min(delay * 2.0, 30.0)
                    continue

                if 500 <= response.status_code < 600:
                    time.sleep(delay)
                    delay = min(delay * 2.0, 20.0)
                    continue

                response.raise_for_status()

            except Exception as error:
                last_error = error
                if attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2.0, 20.0)

    raise RuntimeError(f"Unable to download public Binance klines: {last_error}")


def download_dataset(
    symbol: str,
    interval: str,
    days: int,
    path: Path,
    refresh: bool = False,
) -> dict[str, Any]:
    """Download only CLOSED spot candles and cache them in project format."""
    if path.exists() and not refresh:
        try:
            with path.open("r", encoding="utf-8") as handle:
                cached = json.load(handle)
            if (
                cached.get("symbol") == symbol
                and cached.get("interval") == interval
                and int(cached.get("requested_days", 0)) >= days
            ):
                return {
                    "symbol": symbol,
                    "interval": interval,
                    "path": str(path),
                    "cached": True,
                    "rows": len(cached.get("timestamps") or []),
                }
        except Exception:
            pass

    now_ms = int(time.time() * 1000)
    end_ms = now_ms - 1_000
    start_ms = end_ms - int(days * 86_400_000)
    cursor = start_ms

    rows: list[list[Any]] = []
    seen: set[int] = set()

    while cursor <= end_ms:
        chunk = _request_klines({
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        })

        if not chunk:
            break

        for item in chunk:
            if not isinstance(item, list) or len(item) < 7:
                continue

            open_time = int(item[0])
            close_time = int(item[6])

            # Exclude the currently forming candle.
            if close_time >= now_ms:
                continue

            if open_time in seen:
                continue
            seen.add(open_time)
            rows.append(item)

        if len(chunk) < 1000:
            break

        next_cursor = int(chunk[-1][0]) + INTERVAL_MS[interval]
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    rows.sort(key=lambda item: int(item[0]))

    payload = {
        "symbol": symbol,
        "interval": interval,
        "requested_days": days,
        "timestamps": [int(r[0]) for r in rows],
        "opens": [float(r[1]) for r in rows],
        "highs": [float(r[2]) for r in rows],
        "lows": [float(r[3]) for r in rows],
        "closes": [float(r[4]) for r in rows],
        "volumes": [float(r[5]) for r in rows],
        "provenance": {
            "source": "BINANCE_PUBLIC_SPOT_KLINES",
            "authenticated": False,
            "closed_candles_only": True,
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "requested_start_ms": start_ms,
            "requested_end_ms": end_ms,
        },
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    temporary.replace(path)

    return {
        "symbol": symbol,
        "interval": interval,
        "path": str(path),
        "cached": False,
        "rows": len(rows),
    }


def _utc_iso(open_time_ms: int) -> str:
    return datetime.fromtimestamp(
        open_time_ms / 1000.0, tz=timezone.utc
    ).isoformat()


def _regime_for(atr_percent: float | None) -> str:
    if atr_percent is None:
        return "UNKNOWN"
    if atr_percent < 0.8:
        return "RANGE"
    if atr_percent > 1.5:
        return "VOLATILE"
    return "TREND"


def run_context_backtest(
    strategy: Any,
    candles: list[Any],
    context_candles: list[Any],
    *,
    max_bars_in_trade: int = 24,
) -> dict[str, Any]:
    """
    Conservative LONG-only runner for v2/v4 strategies that require 4H context.

    It mirrors api.strategy_engine.backtest_runner:
      closed candles only, one position, stop before target on ambiguous bar,
      costs charged on close. Quantity/risk from precision-aware diagnostics
      are used when the strategy provides them.
    """
    from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
    from api.strategy_supervisor.stats import ClosedTrade

    cost_config = TradingCostConfig()
    trades: list[ClosedTrade] = []
    reason_counts: dict[str, int] = {}
    signals = 0
    open_trade: dict[str, Any] | None = None

    for index, candle in enumerate(candles):
        if open_trade is not None:
            stop = open_trade["stop"]
            target = open_trade["target"]
            exit_price = None

            if candle.low <= stop:
                exit_price = stop
            elif candle.high >= target:
                exit_price = target
            elif index - open_trade["entry_index"] >= max_bars_in_trade:
                exit_price = candle.close

            if exit_price is not None:
                costs = compute_trade_costs(
                    entry_price=open_trade["entry"],
                    exit_price=exit_price,
                    quantity=open_trade["quantity"],
                    side="LONG",
                    config=cost_config,
                )
                risk = open_trade["risk_amount"]
                trades.append(
                    ClosedTrade(
                        strategy_id=strategy.strategy_key,
                        closed_at_utc=_utc_iso(candle.open_time_ms),
                        regime=open_trade["regime"],
                        net_pnl=costs["net_pnl"],
                        r_multiple=(
                            costs["net_pnl"] / risk if risk and risk > 0 else None
                        ),
                    )
                )
                open_trade = None

            if open_trade is not None:
                continue

        decision = strategy.evaluate_with_context(candles, index, context_candles)
        reason_counts[decision.reason_code] = (
            reason_counts.get(decision.reason_code, 0) + 1
        )

        if not decision.is_trade:
            continue

        signals += 1
        entry = float(decision.entry)
        stop = float(decision.stop)
        target = float(decision.take_profit_2)

        if not (stop < entry < target):
            continue

        diagnostics = decision.diagnostics or {}
        stop_distance = entry - stop

        try:
            quantity = float(diagnostics.get("quantity"))
        except (TypeError, ValueError):
            quantity = 1.0 / stop_distance

        if not math.isfinite(quantity) or quantity <= 0:
            continue

        try:
            risk_amount = float(diagnostics.get("actual_risk_usd"))
        except (TypeError, ValueError):
            risk_amount = 1.0

        if not math.isfinite(risk_amount) or risk_amount <= 0:
            risk_amount = 1.0

        open_trade = {
            "entry": entry,
            "stop": stop,
            "target": target,
            "quantity": quantity,
            "risk_amount": risk_amount,
            "entry_index": index,
            "regime": _regime_for(diagnostics.get("atr_percent")),
        }

    return {
        "strategy_key": strategy.strategy_key,
        "version": strategy.version,
        "parameter_fingerprint": strategy.config.fingerprint(),
        "candles": len(candles),
        "signals": signals,
        "closed_trades": len(trades),
        "still_open_at_end": open_trade is not None,
        "trades": trades,
        "reason_counts": dict(
            sorted(reason_counts.items(), key=lambda pair: -pair[1])[:12]
        ),
        "cost_config": cost_config.snapshot(),
    }


def _sample_id(
    strategy_key: str,
    symbol: str,
    timeframe: str,
    dataset_hashes: list[str],
    parameter_fingerprint: str,
) -> str:
    payload = "|".join(
        [strategy_key, symbol, timeframe, parameter_fingerprint, *dataset_hashes]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _research_status(
    stats: dict[str, Any],
    validation: dict[str, Any],
    gates: dict[str, Any],
) -> str:
    if gates.get("passed") is True:
        return PROMOTION_PASSED

    # Research-only early screen. It NEVER overrides repository promotion gates.
    pf = validation.get("oos_profit_factor")
    pnl = validation.get("oos_net_pnl")
    exp = validation.get("oos_expectancy_r")
    dd = validation.get("oos_max_drawdown_r")
    robust = validation.get("robustness_ratio")
    oos = validation.get("oos_trades") or 0

    promising = (
        oos >= 15
        and isinstance(pnl, (int, float)) and pnl > 0
        and isinstance(pf, (int, float)) and pf >= 1.05
        and isinstance(exp, (int, float)) and exp > 0
        and isinstance(dd, (int, float)) and dd <= 10.0
        and isinstance(robust, (int, float)) and robust >= 0.50
        and validation.get("walk_forward_passed") is True
        and validation.get("look_ahead_leakage") is False
    )

    return RESEARCH_SHORTLIST if promising else REJECT


def _evaluate_result(
    *,
    symbol: str,
    timeframe: str,
    strategy: Any,
    backtest: dict[str, Any],
    dataset_hashes: list[str],
) -> dict[str, Any]:
    from api.strategy_supervisor.gates import promotion_gates
    from api.strategy_supervisor.stats import build_stats
    from api.strategy_supervisor.validation import validate_candidate

    trades = backtest["trades"]
    stats = build_stats(trades)

    sample_id = _sample_id(
        strategy.strategy_key,
        symbol,
        timeframe,
        dataset_hashes,
        backtest["parameter_fingerprint"],
    )

    validation = validate_candidate(
        f"{strategy.strategy_key}:{symbol}",
        trades,
        sample_id=sample_id,
        holdout_fraction=0.30,
        window_count=4,
        safety_violations=(),
    )
    gates = promotion_gates(validation)
    status = _research_status(stats, validation, gates)

    return {
        "candidate": f"{strategy.strategy_key}:{symbol}",
        "strategy_key": strategy.strategy_key,
        "strategy_version": strategy.version,
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "promotion_passed": bool(gates.get("passed")),
        "stats": stats,
        "validation": validation,
        "promotion_gates": gates,
        "signals": backtest["signals"],
        "closed_trades": backtest["closed_trades"],
        "top_reason_counts": backtest["reason_counts"],
        "parameter_fingerprint": backtest["parameter_fingerprint"],
        "sample_id": sample_id,
        "real_orders_enabled": False,
    }


def analyze_symbol(job: dict[str, Any]) -> list[dict[str, Any]]:
    """CPU worker: evaluate all eligible strategies for one symbol."""
    from api.strategy_engine.backtest_runner import run_backtest
    from api.strategy_engine.dataset_quality import (
        audit_dataset,
        load_research_candles,
        quality_verdict,
    )
    from api.strategy_engine.strategies import (
        LondonSessionBreakoutRetest,
        SessionVwapTrendPullback,
        TrendPullbackEmaStructure,
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
    paths = {k: Path(v) for k, v in job["paths"].items()}
    results: list[dict[str, Any]] = []

    audits: dict[str, dict[str, Any]] = {}
    candles: dict[str, list[Any]] = {}

    for interval, path in paths.items():
        report = audit_dataset(path)
        verdict = quality_verdict(report)
        audits[interval] = {
            "report": report,
            "verdict": verdict,
        }
        if verdict.get("usable"):
            candles[interval] = load_research_candles(path)

    def usable(*intervals: str) -> bool:
        return all(
            interval in candles and len(candles[interval]) > 100
            for interval in intervals
        )

    # v1 is valid only on instruments whose real tick size is 0.01.
    if symbol not in PRECISION_SENSITIVE and usable("5m"):
        for cls in (
            SessionVwapTrendPullback,
            LondonSessionBreakoutRetest,
            TrendPullbackEmaStructure,
        ):
            strategy = cls()
            bt = run_backtest(strategy, candles["5m"])
            results.append(
                _evaluate_result(
                    symbol=symbol,
                    timeframe="5m",
                    strategy=strategy,
                    backtest=bt,
                    dataset_hashes=[audits["5m"]["report"]["file_sha256"]],
                )
            )

    # v2: 1H execution + closed 4H context.
    if symbol not in PRECISION_SENSITIVE and usable("1h", "4h"):
        for cls in (
            SessionVwapTrendPullbackV2,
            LondonSessionBreakoutRetestV2,
            TrendPullbackEmaStructureV2,
        ):
            strategy = cls()
            bt = run_context_backtest(
                strategy,
                candles["1h"],
                candles["4h"],
                max_bars_in_trade=24,
            )
            results.append(
                _evaluate_result(
                    symbol=symbol,
                    timeframe="1h+4h",
                    strategy=strategy,
                    backtest=bt,
                    dataset_hashes=[
                        audits["1h"]["report"]["file_sha256"],
                        audits["4h"]["report"]["file_sha256"],
                    ],
                )
            )

    # Precision-aware @4.0.0 is valid for every instrument in the fixture.
    if usable("1h", "4h"):
        strategy = SessionVwapRangeLowVolPrecision(symbol=symbol)
        bt = run_context_backtest(
            strategy,
            candles["1h"],
            candles["4h"],
            max_bars_in_trade=24,
        )
        results.append(
            _evaluate_result(
                symbol=symbol,
                timeframe="1h+4h",
                strategy=strategy,
                backtest=bt,
                dataset_hashes=[
                    audits["1h"]["report"]["file_sha256"],
                    audits["4h"]["report"]["file_sha256"],
                ],
            )
        )

    if not results:
        results.append({
            "candidate": f"DATA:{symbol}",
            "strategy_key": "DATA_QUALITY",
            "symbol": symbol,
            "status": REJECT,
            "error": "No usable dataset/strategy combination",
            "dataset_audits": audits,
            "real_orders_enabled": False,
        })

    return results


def _rank_key(item: dict[str, Any]) -> tuple:
    validation = item.get("validation") or {}
    stats = item.get("stats") or {}

    pf = validation.get("oos_profit_factor")
    exp = validation.get("oos_expectancy_r")
    pnl = validation.get("oos_net_pnl")
    robust = validation.get("robustness_ratio")
    dd = validation.get("oos_max_drawdown_r")
    oos = validation.get("oos_trades") or 0

    def number(value: Any, fallback: float = -1e9) -> float:
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else fallback

    status_weight = {
        PROMOTION_PASSED: 3,
        RESEARCH_SHORTLIST: 2,
        REJECT: 1,
    }.get(item.get("status"), 0)

    return (
        status_weight,
        number(pf),
        number(exp),
        number(robust),
        number(pnl),
        oos,
        -number(dd, fallback=1e9),
        stats.get("closed_trades") or 0,
    )


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 88)
    lines.append("TRADINGCORE STRATEGY LAB")
    lines.append("=" * 88)
    lines.append(f"Generated UTC: {report['generated_at_utc']}")
    lines.append(f"Days: {report['days']}")
    lines.append(f"Symbols: {', '.join(report['symbols'])}")
    lines.append("MODE: RESEARCH ONLY | LIVE ORDERS: IMPOSSIBLE FROM THIS SCRIPT")
    lines.append("")

    counts = report["counts"]
    lines.append(
        f"Candidates={counts['total']}  "
        f"promotion_passed={counts['promotion_passed']}  "
        f"research_shortlist={counts['research_shortlist']}  "
        f"rejected_or_more_data={counts['rejected_or_more_data']}"
    )
    lines.append("")

    for index, item in enumerate(report["ranking"], start=1):
        validation = item.get("validation") or {}
        stats = item.get("stats") or {}
        gates = item.get("promotion_gates") or {}

        lines.append(
            f"{index:02d}. {item.get('candidate')}  [{item.get('status')}]"
        )
        lines.append(
            "    "
            f"tf={item.get('timeframe')} "
            f"trades={stats.get('closed_trades')} "
            f"OOS={validation.get('oos_trades')} "
            f"PF={validation.get('oos_profit_factor')} "
            f"expR={validation.get('oos_expectancy_r')} "
            f"net={validation.get('oos_net_pnl')} "
            f"DD_R={validation.get('oos_max_drawdown_r')} "
            f"robust={validation.get('robustness_ratio')} "
            f"WF={validation.get('walk_forward_passed')}"
        )
        failed = gates.get("failed_gates") or []
        if failed:
            lines.append(f"    failed_gates={','.join(failed)}")
        lines.append("")

    lines.append("-" * 88)
    lines.append("IMPORTANT: promotion gate PASS is a research result, not permission for LIVE.")
    lines.append("A separate live-PAPER confirmation and explicit owner approval remain required.")
    lines.append("-" * 88)
    return "\n".join(lines)


def build_jobs(
    symbols: list[str],
    days: int,
    dataset_dir: Path,
    refresh: bool,
    download_workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requests_to_make: list[tuple[str, str, Path]] = []

    for symbol in symbols:
        intervals = ["1h", "4h"]
        if symbol not in PRECISION_SENSITIVE:
            intervals.insert(0, "5m")

        for interval in intervals:
            path = dataset_dir / f"{symbol}_{interval}_{days}d.json"
            requests_to_make.append((symbol, interval, path))

    download_results: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, download_workers)) as pool:
        future_map = {
            pool.submit(
                download_dataset, symbol, interval, days, path, refresh
            ): (symbol, interval, path)
            for symbol, interval, path in requests_to_make
        }

        for future in as_completed(future_map):
            symbol, interval, path = future_map[future]
            try:
                result = future.result()
                download_results.append(result)
                print(
                    f"[DATA] {symbol} {interval}: rows={result['rows']} "
                    f"cached={result['cached']}",
                    flush=True,
                )
            except Exception as error:
                download_results.append({
                    "symbol": symbol,
                    "interval": interval,
                    "path": str(path),
                    "error": f"{type(error).__name__}: {error}",
                })
                print(
                    f"[DATA] {symbol} {interval}: FAILED {error}",
                    file=sys.stderr,
                    flush=True,
                )

    jobs = []
    for symbol in symbols:
        paths = {}
        for item in download_results:
            if item.get("symbol") == symbol and not item.get("error"):
                paths[item["interval"]] = item["path"]
        jobs.append({"symbol": symbol, "paths": paths})

    return jobs, download_results


def main() -> int:
    parser = argparse.ArgumentParser(description="TradingCore parallel Strategy Lab")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--download-workers", type=int, default=3)
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--output",
        default="strategy_lab_results",
        help="output directory relative to repository (or absolute)",
    )
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if args.days < 30:
        raise SystemExit("--days must be >= 30")
    if args.workers < 1 or args.download_workers < 1:
        raise SystemExit("worker counts must be >= 1")

    # Refuse unsafe project mode even though this script itself has no order path.
    from config.startup_safety import assert_safe_startup
    safety = assert_safe_startup()

    symbols = [str(item).strip().upper() for item in args.symbols if str(item).strip()]
    symbols = [symbol for symbol in symbols if symbol in DEFAULT_SYMBOLS]
    if not symbols:
        raise SystemExit("No supported symbols selected")

    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    dataset_dir = output_dir / "datasets"
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 88)
    print("TRADINGCORE STRATEGY LAB - PARALLEL RESEARCH")
    print("=" * 88)
    print("Safety:", safety)
    print("Symbols:", ", ".join(symbols))
    print("Days:", args.days)
    print("Real orders: DISABLED / no order code in orchestrator")
    print("=" * 88, flush=True)

    jobs, downloads = build_jobs(
        symbols,
        args.days,
        dataset_dir,
        args.refresh,
        args.download_workers,
    )

    all_results: list[dict[str, Any]] = []
    worker_count = min(max(1, args.workers), len(jobs))

    with ProcessPoolExecutor(max_workers=worker_count) as pool:
        future_map = {
            pool.submit(analyze_symbol, job): job["symbol"]
            for job in jobs
        }

        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                result = future.result()
                all_results.extend(result)
                print(
                    f"[ANALYST] {symbol}: {len(result)} candidate result(s)",
                    flush=True,
                )
            except Exception as error:
                all_results.append({
                    "candidate": f"WORKER:{symbol}",
                    "strategy_key": "WORKER_ERROR",
                    "symbol": symbol,
                    "status": REJECT,
                    "error": f"{type(error).__name__}: {error}",
                    "real_orders_enabled": False,
                })
                print(
                    f"[ANALYST] {symbol}: FAILED {error}",
                    file=sys.stderr,
                    flush=True,
                )

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
            "promotion_passed": sum(
                1 for item in ranking if item.get("status") == PROMOTION_PASSED
            ),
            "research_shortlist": sum(
                1 for item in ranking if item.get("status") == RESEARCH_SHORTLIST
            ),
            "rejected_or_more_data": sum(
                1 for item in ranking if item.get("status") == REJECT
            ),
        },
        "ranking": ranking,
        "notes": [
            "Repository promotion gates are authoritative.",
            "RESEARCH_SHORTLIST_NOT_LIVE is an early screen only and cannot promote a strategy.",
            "ADA/XRP/TRX are excluded from v1/v2 because two-decimal rounding was invalidated; @4.0.0 is precision-aware.",
            "No parameter optimisation is performed.",
        ],
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"strategy_lab_{stamp}.json"
    text_path = output_dir / f"strategy_lab_{stamp}.txt"
    latest_json = output_dir / "LATEST.json"
    latest_text = output_dir / "LATEST.txt"

    text = render_text(report)

    for path in (json_path, latest_json):
        with path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)

    for path in (text_path, latest_text):
        path.write_text(text, encoding="utf-8")

    print("")
    print(text)
    print("")
    print(f"JSON: {json_path}")
    print(f"TEXT: {text_path}")
    print(f"LATEST: {latest_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
