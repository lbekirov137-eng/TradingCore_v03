#!/usr/bin/env python3
"""TradingCore Delta-Neutral Funding Carry Lab V1.

Long spot + short USDT perpetual with equal notionals. A small frozen family of
funding persistence gates is selected on a three-year discovery window and
validated once on an untouched final-year holdout, independently on Binance and
OKX. Decisions use only realized funding and basis already known; entries and
exits occur at the next 8h bar open. Conservative TradingCore costs are applied
to both legs. Capital is split 50/50 between spot and perpetual collateral;
perpetual notional equals collateral (1x on that leg).

Research/PAPER only. No private APIs, balances, credentials, transfers or orders.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_DELTA_NEUTRAL_FUNDING_CARRY_V1"
BIN_SPOT = "https://data-api.binance.vision"
BIN_FUT = "https://fapi.binance.com"
OKX = "https://www.okx.com"
SYMBOLS = ("BTC", "ETH", "SOL")
YEARS = 4
DAY_MS = 86_400_000
BAR_MS = 8 * 3_600_000
DISCOVERY_DAYS = 3 * 365
HOLDOUT_DAYS = 365
PURGE_BARS = 2
CAPITAL_USD = 1000.0
LEG_NOTIONAL_USD = 500.0
MAX_HOLD_BARS = 90  # 30 days at 8h per bar
MAX_ENTRY_BASIS = 0.02
MIN_ENTRY_BASIS = -0.0025
BASIS_STOP_WIDENING = 0.025

# Frozen structural hypotheses. Per-interval rates: 0.0001 = 1 bp.
LANES = (
    {"lane_id": "AVG3_GT_1_5BP", "lookback": 3, "threshold": 0.00015},
    {"lane_id": "AVG6_GT_1_5BP", "lookback": 6, "threshold": 0.00015},
    {"lane_id": "AVG3_GT_3BP", "lookback": 3, "threshold": 0.00030},
    {"lane_id": "AVG6_GT_3BP", "lookback": 6, "threshold": 0.00030},
)


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def req(url: str, attempts: int = 6) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "TradingCore-FundingCarry/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError(last)


def bounds() -> tuple[int, int]:
    now = int(time.time() * 1000)
    end = (now // BAR_MS) * BAR_MS - 1
    return end - int(YEARS * 365.25 * DAY_MS), end


def fetch_binance_klines(symbol: str, futures: bool) -> list[dict[str, float | int]]:
    start, end = bounds()
    cursor = start
    rows: dict[int, dict[str, float | int]] = {}
    base = BIN_FUT if futures else BIN_SPOT
    path = "/fapi/v1/klines" if futures else "/api/v3/klines"
    while cursor <= end:
        query = urlencode({"symbol": f"{symbol}USDT", "interval": "8h", "startTime": cursor, "endTime": end, "limit": 1000})
        batch = req(f"{base}{path}?{query}")
        if not isinstance(batch, list) or not batch:
            break
        last_open: int | None = None
        for item in batch:
            try:
                timestamp = int(item[0])
                close_timestamp = int(item[6])
                last_open = timestamp
                if start <= timestamp and close_timestamp <= end:
                    rows[timestamp] = {
                        "ts": timestamp,
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                    }
            except Exception:
                pass
        if last_open is None or len(batch) < 1000:
            break
        nxt = last_open + BAR_MS
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.02)
    return [rows[key] for key in sorted(rows)]


def fetch_binance_funding(symbol: str) -> list[tuple[int, float]]:
    start, end = bounds()
    cursor = start
    values: dict[int, float] = {}
    while cursor <= end:
        query = urlencode({"symbol": f"{symbol}USDT", "startTime": cursor, "endTime": end, "limit": 1000})
        batch = req(f"{BIN_FUT}/fapi/v1/fundingRate?{query}")
        if not isinstance(batch, list) or not batch:
            break
        last_ts: int | None = None
        for item in batch:
            try:
                timestamp = int(item["fundingTime"])
                last_ts = timestamp
                if start <= timestamp <= end:
                    values[timestamp] = float(item["fundingRate"])
            except Exception:
                pass
        if last_ts is None or len(batch) < 1000:
            break
        nxt = last_ts + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.02)
    return sorted(values.items())


def fetch_okx_klines(symbol: str, swap: bool) -> list[dict[str, float | int]]:
    start, end = bounds()
    instrument = f"{symbol}-USDT-SWAP" if swap else f"{symbol}-USDT"
    cursor: int | None = None
    rows: dict[int, dict[str, float | int]] = {}
    for _ in range(700):
        params: dict[str, Any] = {"instId": instrument, "bar": "8H", "limit": 100}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = req(f"{OKX}/api/v5/market/history-candles?{urlencode(params)}")
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise RuntimeError(payload)
        batch = payload.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                timestamp = int(item[0])
                oldest = timestamp if oldest is None else min(oldest, timestamp)
                confirmed = str(item[8]) == "1" if len(item) > 8 else True
                if confirmed and start <= timestamp <= end:
                    rows[timestamp] = {
                        "ts": timestamp,
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                    }
            except Exception:
                pass
        if oldest is None or oldest <= start or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return [rows[key] for key in sorted(rows)]


def fetch_okx_funding(symbol: str) -> list[tuple[int, float]]:
    start, end = bounds()
    instrument = f"{symbol}-USDT-SWAP"
    cursor: int | None = None
    values: dict[int, float] = {}
    for _ in range(500):
        params: dict[str, Any] = {"instId": instrument, "limit": 400}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = req(f"{OKX}/api/v5/public/funding-rate-history?{urlencode(params)}")
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise RuntimeError(payload)
        batch = payload.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                timestamp = int(item["fundingTime"])
                oldest = timestamp if oldest is None else min(oldest, timestamp)
                if start <= timestamp <= end:
                    values[timestamp] = float(item.get("realizedRate") or item.get("fundingRate"))
            except Exception:
                pass
        if oldest is None or oldest <= start or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return sorted(values.items())


def align(spot: list[dict[str, Any]], perp: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spot_by_ts = {int(row["ts"]): row for row in spot}
    perp_by_ts = {int(row["ts"]): row for row in perp}
    timestamps = sorted(set(spot_by_ts) & set(perp_by_ts))
    return [
        {
            "ts": timestamp,
            "spot": spot_by_ts[timestamp],
            "perp": perp_by_ts[timestamp],
            "basis": float(perp_by_ts[timestamp]["close"]) / float(spot_by_ts[timestamp]["close"]) - 1.0,
        }
        for timestamp in timestamps
    ]


def funding_at_or_before(funding: list[tuple[int, float]], timestamp: int) -> float | None:
    times = [item[0] for item in funding]
    index = bisect.bisect_right(times, timestamp) - 1
    return funding[index][1] if index >= 0 else None


def stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = peak = max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(100 * len(wins) / n, 2) if n else None,
        "net_return_pct": round(100 * sum(values), 4) if n else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else (99.0 if wins else None),
        "average_return_bps": round(10_000 * sum(values) / n, 4) if n else None,
        "max_drawdown_pct": round(100 * max_dd, 4) if n else None,
    }


def simulate(
    lane: dict[str, Any],
    market: list[dict[str, Any]],
    funding: list[tuple[int, float]],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    config = TradingCostConfig()
    rates: list[float | None] = [funding_at_or_before(funding, int(row["ts"])) for row in market]
    position: dict[str, Any] | None = None
    pending_entry = False
    pending_exit = False
    trade_returns: list[float] = []
    trades: list[dict[str, Any]] = []
    signals = 0

    for index, row in enumerate(market):
        timestamp = int(row["ts"])
        spot_open = float(row["spot"]["open"])
        perp_open = float(row["perp"]["open"])

        if pending_exit and position is not None:
            spot_cost = compute_trade_costs(
                entry_price=float(position["spot_entry"]), exit_price=spot_open,
                quantity=float(position["spot_quantity"]), side="LONG", config=config,
            )
            perp_cost = compute_trade_costs(
                entry_price=float(position["perp_entry"]), exit_price=perp_open,
                quantity=float(position["perp_quantity"]), side="SHORT", config=config,
            )
            total_pnl = float(spot_cost["net_pnl"]) + float(perp_cost["net_pnl"]) + float(position["funding_pnl"])
            trade_return = total_pnl / CAPITAL_USD
            trade_returns.append(trade_return)
            trades.append(
                {
                    "entry_utc": datetime.fromtimestamp(int(position["entry_ts"]) / 1000, tz=timezone.utc).isoformat(),
                    "exit_utc": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat(),
                    "holding_bars": index - int(position["entry_index"]),
                    "funding_pnl": round(float(position["funding_pnl"]), 6),
                    "return_pct": round(100 * trade_return, 5),
                    "reason": str(position.get("exit_reason") or "EXIT"),
                }
            )
            position = None
            pending_exit = False

        if pending_entry and position is None:
            spot_quantity = LEG_NOTIONAL_USD / spot_open
            perp_quantity = LEG_NOTIONAL_USD / perp_open
            position = {
                "entry_ts": timestamp,
                "entry_index": index,
                "spot_entry": spot_open,
                "perp_entry": perp_open,
                "spot_quantity": spot_quantity,
                "perp_quantity": perp_quantity,
                "entry_basis": perp_open / spot_open - 1.0,
                "funding_pnl": 0.0,
            }
            pending_entry = False

        if position is not None:
            rate = rates[index]
            # Positive realized funding is paid by longs to our short perp leg.
            if finite(rate):
                position["funding_pnl"] = float(position["funding_pnl"]) + LEG_NOTIONAL_USD * float(rate)
            current_basis = float(row["perp"]["close"]) / float(row["spot"]["close"]) - 1.0
            lookback = int(lane["lookback"])
            recent = [value for value in rates[max(0, index - lookback + 1): index + 1] if finite(value)]
            rolling = sum(float(value) for value in recent) / len(recent) if len(recent) == lookback else None
            holding = index - int(position["entry_index"])
            if holding >= MAX_HOLD_BARS:
                position["exit_reason"] = "MAX_HOLD"
                pending_exit = True
            elif current_basis - float(position["entry_basis"]) >= BASIS_STOP_WIDENING:
                position["exit_reason"] = "BASIS_STOP"
                pending_exit = True
            elif finite(rolling) and float(rolling) <= 0:
                position["exit_reason"] = "FUNDING_DECAY"
                pending_exit = True

        if (
            position is None and not pending_entry and not pending_exit
            and start_ms <= timestamp <= end_ms
        ):
            lookback = int(lane["lookback"])
            recent = [value for value in rates[max(0, index - lookback + 1): index + 1] if finite(value)]
            rolling = sum(float(value) for value in recent) / len(recent) if len(recent) == lookback else None
            basis = float(row["basis"])
            if (
                finite(rolling)
                and float(rolling) >= float(lane["threshold"])
                and MIN_ENTRY_BASIS <= basis <= MAX_ENTRY_BASIS
                and index + 1 < len(market)
            ):
                signals += 1
                pending_entry = True

    result_stats = stats(trade_returns)
    return {"signals": signals, "stats": result_stats, "recent_trades": trades[-15:]}


def discovery_segments(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    width = end_ms - start_ms + 1
    return [
        (
            start_ms + (width * index) // 3,
            start_ms + (width * (index + 1)) // 3 - 1,
        )
        for index in range(3)
    ]


def discovery_checks(full: dict[str, Any], positive_segments: int) -> dict[str, bool]:
    row = full["stats"]
    return {
        "trades": int(row.get("closed_trades") or 0) >= 6,
        "profit_factor": finite(row.get("profit_factor")) and float(row["profit_factor"]) >= 1.15,
        "average_return": finite(row.get("average_return_bps")) and float(row["average_return_bps"]) > 0,
        "drawdown": finite(row.get("max_drawdown_pct")) and float(row["max_drawdown_pct"]) <= 10.0,
        "segments": positive_segments >= 2,
    }


def holdout_checks(full: dict[str, Any]) -> dict[str, bool]:
    row = full["stats"]
    return {
        "trades": int(row.get("closed_trades") or 0) >= 2,
        "net_positive": finite(row.get("net_return_pct")) and float(row["net_return_pct"]) > 0,
        "profit_factor": finite(row.get("profit_factor")) and float(row["profit_factor"]) > 1.0,
        "drawdown": finite(row.get("max_drawdown_pct")) and float(row["max_drawdown_pct"]) <= 5.0,
    }


def rank_candidate(payload: dict[str, Any]) -> tuple[float, float, int]:
    averages: list[float] = []
    pfs: list[float] = []
    trades: list[int] = []
    for venue in ("BINANCE", "OKX"):
        row = payload["venues"][venue]["discovery"]["stats"]
        averages.append(float(row["average_return_bps"]) if finite(row.get("average_return_bps")) else -999.0)
        pfs.append(float(row["profit_factor"]) if finite(row.get("profit_factor")) else -999.0)
        trades.append(int(row.get("closed_trades") or 0))
    return min(averages), min(pfs), min(trades)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="funding_carry_runtime")
    args = parser.parse_args()
    safety = assert_safe_startup()

    loaders: dict[str, tuple[Callable[[str, bool], list[dict[str, Any]]] | None, Any]] = {}
    market_data: dict[tuple[str, str], list[dict[str, Any]]] = {}
    funding_data: dict[tuple[str, str], list[tuple[int, float]]] = {}
    failures: dict[str, str] = {}

    for venue in ("BINANCE", "OKX"):
        for symbol in SYMBOLS:
            try:
                if venue == "BINANCE":
                    spot = fetch_binance_klines(symbol, False)
                    perp = fetch_binance_klines(symbol, True)
                    funding = fetch_binance_funding(symbol)
                else:
                    spot = fetch_okx_klines(symbol, False)
                    perp = fetch_okx_klines(symbol, True)
                    funding = fetch_okx_funding(symbol)
                aligned = align(spot, perp)
                market_data[(venue, symbol)] = aligned
                funding_data[(venue, symbol)] = funding
                print("FUNDING_DATA", venue, symbol, "bars=", len(aligned), "funding=", len(funding), flush=True)
            except Exception as exc:
                failures[f"{venue}:{symbol}"] = f"{type(exc).__name__}: {exc}"
                print("FUNDING_DATA_FAIL", venue, symbol, exc, flush=True)

    available = [rows for rows in market_data.values() if rows]
    if not available:
        raise RuntimeError("No aligned carry market data")
    common_end = min(int(rows[-1]["ts"]) for rows in available)
    holdout_start = common_end - HOLDOUT_DAYS * DAY_MS
    discovery_start = holdout_start - DISCOVERY_DAYS * DAY_MS
    discovery_end = holdout_start - PURGE_BARS * BAR_MS - 1
    holdout_start_purged = holdout_start + PURGE_BARS * BAR_MS
    segments = discovery_segments(discovery_start, discovery_end)

    results: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for lane in LANES:
            venues: dict[str, Any] = {}
            discovery_passed_both = True
            for venue in ("BINANCE", "OKX"):
                market = market_data.get((venue, symbol)) or []
                funding = funding_data.get((venue, symbol)) or []
                if len(market) < 1000 or len(funding) < 500:
                    venues[venue] = {"error": "INSUFFICIENT_DATA", "discovery_passed": False, "holdout_passed": False}
                    discovery_passed_both = False
                    continue
                discovery = simulate(lane, market, funding, discovery_start, discovery_end)
                segment_rows = [simulate(lane, market, funding, start, end) for start, end in segments]
                positive_segments = sum(
                    1 for segment in segment_rows
                    if finite(segment["stats"].get("net_return_pct")) and float(segment["stats"]["net_return_pct"]) > 0
                )
                dchecks = discovery_checks(discovery, positive_segments)
                holdout = simulate(lane, market, funding, holdout_start_purged, common_end)
                hchecks = holdout_checks(holdout)
                venues[venue] = {
                    "discovery": discovery,
                    "discovery_segments": segment_rows,
                    "positive_discovery_segments": positive_segments,
                    "discovery_checks": dchecks,
                    "discovery_passed": all(dchecks.values()),
                    "holdout": holdout,
                    "holdout_checks": hchecks,
                    "holdout_passed": all(hchecks.values()),
                }
                discovery_passed_both = discovery_passed_both and all(dchecks.values())
            results.append({"symbol": symbol, **lane, "venues": venues, "discovery_passed_both_venues": discovery_passed_both})

    discovery_candidates = [row for row in results if row["discovery_passed_both_venues"]]
    discovery_candidates.sort(key=rank_candidate, reverse=True)
    selected = discovery_candidates[0] if discovery_candidates else None
    selected_holdout_both = bool(
        selected and all(bool(selected["venues"][venue].get("holdout_passed")) for venue in ("BINANCE", "OKX"))
    )
    if selected is None:
        state = "NO_FUNDING_CARRY_CANDIDATE"
    elif selected_holdout_both:
        state = "FUNDING_CARRY_CANDIDATE_FOUND_FORWARD_REQUIRED"
    else:
        state = "FUNDING_CARRY_DISCOVERY_CANDIDATE_FAILED_HOLDOUT"

    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "history_years": YEARS,
        "capital_usd": CAPITAL_USD,
        "leg_notional_usd": LEG_NOTIONAL_USD,
        "frozen_lanes": list(LANES),
        "execution_model": "next-8h-bar open; long spot + equal-notional short perpetual; realized funding; conservative TradingCore costs on both legs; no leverage above 1x per leg",
        "selected": ({"symbol": selected["symbol"], "lane_id": selected["lane_id"]} if selected else None),
        "selected_holdout_passed_both_venues": selected_holdout_both,
        "discovery_candidates": [{"symbol": row["symbol"], "lane_id": row["lane_id"]} for row in discovery_candidates],
        "results": results,
        "data_failures": failures,
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "live_permission": False,
        "note": "Historical discovery+holdout only. A pass still requires fresh funding/basis forward execution evidence and exchange/counterparty risk review before any micro-live decision.",
    }
    output = Path(args.output_dir)
    atomic_json(output / "FUNDING_CARRY_RESULT.json", report)
    print("FUNDING_CARRY", state, "selected=", report["selected"], "holdout_both=", selected_holdout_both, "failures=", failures, "LIVE=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
