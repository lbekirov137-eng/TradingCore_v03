#!/usr/bin/env python3
"""Run the frozen 36-lane Fast PAPER set through a 365-day two-venue replay.

Public closed spot candles only. No API keys, balances, order clients, parameter
tuning, or LIVE path. Trading costs come from TradingCore's existing cost model
through fast_paper_cloud_once.simulate_lane().
"""
from __future__ import annotations

import argparse
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import fast_paper_cloud_once as fast
from api.strategy_engine.strategies.contracts import Candle
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from config.startup_safety import assert_safe_startup
from fast_paper_protocol import HYPOTHESES, SYMBOLS, TIMEFRAMES
from rapid_replay_protocol import (
    HISTORY_DAYS,
    MAX_FULL_DD_R,
    MIN_FULL_EXPECTANCY_R,
    MIN_FULL_PF,
    MIN_HALF_EXPECTANCY_R,
    MIN_HALF_PF,
    MIN_SEGMENT_ROBUSTNESS,
    MIN_TRADES_PER_HALF,
    MIN_TRADES_PER_VENUE,
    PROTOCOL_FINGERPRINT,
    PROTOCOL_VERSION,
    protocol_dict,
)

DAY_MS = 86_400_000
BINANCE_BASE = "https://data-api.binance.vision"
BYBIT_BASE = "https://api.bybit.com"
SCHEMA = "TRADINGCORE_RAPID_REPLAY_V1"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def request_json(url: str, attempts: int = 6) -> Any:
    last: Exception | None = None
    delay = 0.5
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "TradingCore-RapidReplay/1.0"})
            with urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay = min(8.0, delay * 2.0)
    raise RuntimeError(f"public market data request failed: {last}")


def interval_meta(timeframe: str) -> tuple[int, str]:
    if timeframe == "15m":
        return 900_000, "15"
    if timeframe == "30m":
        return 1_800_000, "30"
    raise ValueError(f"unsupported timeframe: {timeframe}")


def closed_window(timeframe: str) -> tuple[int, int]:
    interval_ms, _ = interval_meta(timeframe)
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // interval_ms) * interval_ms - 1
    start_ms = end_ms - HISTORY_DAYS * DAY_MS
    return start_ms, end_ms


def fetch_binance(symbol: str, timeframe: str) -> list[Candle]:
    interval_ms, _ = interval_meta(timeframe)
    start_ms, end_ms = closed_window(timeframe)
    cursor = start_ms
    by_time: dict[int, Candle] = {}
    while cursor <= end_ms:
        query = urlencode({"symbol": symbol, "interval": timeframe, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        payload = request_json(f"{BINANCE_BASE}/api/v3/klines?{query}")
        if not isinstance(payload, list) or not payload:
            break
        last_open = None
        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                continue
            try:
                open_ms = int(row[0]); close_ms = int(row[6])
                if open_ms < start_ms or close_ms > end_ms:
                    continue
                by_time[open_ms] = Candle(open_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                last_open = open_ms
            except (TypeError, ValueError):
                continue
        if last_open is None or len(payload) < 1000:
            break
        nxt = int(last_open) + interval_ms
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.02)
    return sorted(by_time.values(), key=lambda c: int(c.open_time_ms))


def fetch_bybit(symbol: str, timeframe: str) -> list[Candle]:
    interval_ms, bybit_interval = interval_meta(timeframe)
    start_ms, end_ms = closed_window(timeframe)
    cursor_end = end_ms
    by_time: dict[int, Candle] = {}
    for _ in range(80):
        query = urlencode({"category": "spot", "symbol": symbol, "interval": bybit_interval, "start": start_ms, "end": cursor_end, "limit": 1000})
        payload = request_json(f"{BYBIT_BASE}/v5/market/kline?{query}")
        if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
            raise RuntimeError(f"Bybit error for {symbol} {timeframe}: {payload}")
        rows = ((payload.get("result") or {}).get("list") or [])
        if not rows:
            break
        oldest = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                open_ms = int(row[0])
                close_ms = open_ms + interval_ms - 1
                if open_ms < start_ms or close_ms > end_ms:
                    continue
                by_time[open_ms] = Candle(open_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                oldest = open_ms if oldest is None else min(oldest, open_ms)
            except (TypeError, ValueError):
                continue
        if oldest is None or oldest <= start_ms or len(rows) < 1000:
            break
        cursor_end = oldest - 1
        time.sleep(0.02)
    return sorted(by_time.values(), key=lambda c: int(c.open_time_ms))


def details_to_trades(lane_id: str, symbol: str, details: list[dict[str, Any]]) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for item in details:
        try:
            stamp = str(item["closed_utc"])
            net = float(item["net_pnl"])
            r = float(item["r_multiple"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(net) and math.isfinite(r):
            out.append(ClosedTrade(lane_id, stamp, symbol, net, r))
    return out


def split_stats(lane: dict[str, Any], start_ms: int, end_ms: int) -> dict[str, Any]:
    trades = details_to_trades(lane["lane_id"], lane["symbol"], lane.get("trades") or [])
    mid_ms = start_ms + (end_ms - start_ms) // 2
    mid_utc = datetime.fromtimestamp(mid_ms / 1000.0, tz=timezone.utc).isoformat()
    first = [t for t in trades if str(t.closed_at_utc) < mid_utc]
    second = [t for t in trades if str(t.closed_at_utc) >= mid_utc]
    return {
        "full": build_stats(trades),
        "first_half": build_stats(first),
        "second_half": build_stats(second),
        "segment_robustness": fast.segment_robustness(trades),
        "midpoint_utc": mid_utc,
    }


def num(stats: dict[str, Any], key: str, fallback: float = -1e99) -> float:
    value = stats.get(key)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return fallback


def venue_checks(stats: dict[str, Any]) -> dict[str, bool]:
    full = stats["full"]; first = stats["first_half"]; second = stats["second_half"]
    robust = stats.get("segment_robustness")
    return {
        "full_trades": int(full.get("closed_trades") or 0) >= MIN_TRADES_PER_VENUE,
        "full_pf": num(full, "profit_factor") >= MIN_FULL_PF,
        "full_expectancy": num(full, "expectancy_r") >= MIN_FULL_EXPECTANCY_R,
        "full_net_positive": num(full, "net_pnl") > 0,
        "full_drawdown": 0 <= num(full, "max_drawdown_r", 1e99) <= MAX_FULL_DD_R,
        "segment_robustness": isinstance(robust, (int, float)) and float(robust) >= MIN_SEGMENT_ROBUSTNESS,
        "first_half_trades": int(first.get("closed_trades") or 0) >= MIN_TRADES_PER_HALF,
        "first_half_pf": num(first, "profit_factor") >= MIN_HALF_PF,
        "first_half_expectancy": num(first, "expectancy_r") > MIN_HALF_EXPECTANCY_R,
        "first_half_net_positive": num(first, "net_pnl") > 0,
        "second_half_trades": int(second.get("closed_trades") or 0) >= MIN_TRADES_PER_HALF,
        "second_half_pf": num(second, "profit_factor") >= MIN_HALF_PF,
        "second_half_expectancy": num(second, "expectancy_r") > MIN_HALF_EXPECTANCY_R,
        "second_half_net_positive": num(second, "net_pnl") > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="rapid_replay_runtime")
    args = parser.parse_args()
    safety = assert_safe_startup()
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)

    # Disable the forward-freeze filter ONLY for this historical replay. Strategy
    # rules, costs, geometry and exits remain exactly the frozen Fast PAPER rules.
    fast.FREEZE_MS = 0

    jobs: list[tuple[str, str, str]] = []
    for venue in ("BINANCE", "BYBIT"):
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                jobs.append((venue, symbol, timeframe))

    market: dict[tuple[str, str, str], list[Candle]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_map = {}
        for venue, symbol, timeframe in jobs:
            fn = fetch_binance if venue == "BINANCE" else fetch_bybit
            future_map[pool.submit(fn, symbol, timeframe)] = (venue, symbol, timeframe)
        for future in as_completed(future_map):
            key = future_map[future]
            label = ":".join(key)
            try:
                rows = future.result()
                market[key] = rows
                print(f"RAPID_DATA {label} bars={len(rows)}", flush=True)
            except Exception as exc:
                failures[label] = f"{type(exc).__name__}: {exc}"
                print(f"RAPID_DATA_FAIL {label} {failures[label]}", flush=True)

    lane_results: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            start_ms, end_ms = closed_window(timeframe)
            for hypothesis in HYPOTHESES:
                lane_id = f"{symbol}:{timeframe}:{hypothesis}"
                venues: dict[str, Any] = {}
                all_pass = True
                for venue in ("BINANCE", "BYBIT"):
                    rows = market.get((venue, symbol, timeframe)) or []
                    if len(rows) < 500:
                        venues[venue] = {"error": "INSUFFICIENT_DATA", "checks": {}, "passed": False}
                        all_pass = False
                        continue
                    lane = fast.simulate_lane(symbol, timeframe, hypothesis, rows)
                    stats = split_stats(lane, start_ms, end_ms)
                    checks = venue_checks(stats)
                    passed = all(checks.values())
                    venues[venue] = {"stats": stats, "checks": checks, "passed": passed}
                    all_pass = all_pass and passed
                state = "RAPID_EVIDENCE_PASS_NOT_LIVE" if all_pass else "RAPID_EVIDENCE_REJECT"
                lane_results.append({"lane_id": lane_id, "state": state, "venues": venues})
                print(f"RAPID_LANE {lane_id} state={state}", flush=True)

    passed = [x for x in lane_results if x["state"] == "RAPID_EVIDENCE_PASS_NOT_LIVE"]
    def rank_key(item: dict[str, Any]) -> tuple:
        vals = []
        exps = []
        trades = 0
        for venue in ("BINANCE", "BYBIT"):
            full = (((item.get("venues") or {}).get(venue) or {}).get("stats") or {}).get("full") or {}
            vals.append(num(full, "profit_factor"))
            exps.append(num(full, "expectancy_r"))
            trades += int(full.get("closed_trades") or 0)
        return (min(vals) if vals else -999, min(exps) if exps else -999, trades)
    passed.sort(key=rank_key, reverse=True)

    report = {
        "schema": SCHEMA,
        "generated_at_utc": now_utc(),
        "state": "RAPID_CANDIDATE_FOUND_NOT_LIVE" if passed else "NO_RAPID_CANDIDATE",
        "protocol": {**protocol_dict(), "fingerprint": PROTOCOL_FINGERPRINT},
        "data_failures": failures,
        "lane_count": len(lane_results),
        "passing_lane_count": len(passed),
        "candidate": passed[0]["lane_id"] if passed else None,
        "passing_lanes": [x["lane_id"] for x in passed],
        "lanes": lane_results,
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "live_permission": False,
        "note": "Rapid historical robustness screen only. A pass is evidence for a micro-live review, not permission or a profit guarantee.",
    }
    atomic_json(out / "RAPID_REPLAY_RESULT.json", report)
    print("=" * 96)
    print("TRADINGCORE RAPID REPLAY FINAL RESULT")
    print("State:", report["state"])
    print("Passing lanes:", len(passed), "/", len(lane_results))
    print("Candidate:", report["candidate"])
    if passed:
        top = passed[0]
        for venue in ("BINANCE", "BYBIT"):
            full = top["venues"][venue]["stats"]["full"]
            print(f"{venue}: trades={full.get('closed_trades')} PF={full.get('profit_factor')} expR={full.get('expectancy_r')} DD={full.get('max_drawdown_r')}")
    print("LIVE / real orders: DISABLED")
    print("Report:", out / "RAPID_REPLAY_RESULT.json")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
