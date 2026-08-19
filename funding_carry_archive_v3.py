#!/usr/bin/env python3
"""TradingCore Delta-Neutral Funding Carry Lab V3.

Uses official Binance Public Data monthly ZIP archives instead of the geo-blocked
fapi REST endpoint, and valid OKX 4H candles. The economic protocol stays frozen:
long spot + short perpetual, equal $500 notionals, no leverage on the perp
collateral, discovery-only selection, purged final-year holdout, next-bar entry,
and conservative fees/slippage on both legs.

Research/PAPER only. No credentials, balances, transfers, or orders.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_DELTA_NEUTRAL_FUNDING_CARRY_V3"
BIN_ARCHIVE = "https://data.binance.vision"
OKX = "https://www.okx.com"
SYMBOL = "BTC"
PAIR = "BTCUSDT"
YEARS = 4
DAY_MS = 86_400_000
BAR_MS = 4 * 3_600_000
DISCOVERY_DAYS = 3 * 365
HOLDOUT_DAYS = 365
PURGE_BARS = 4
CAPITAL_USD = 1000.0
LEG_NOTIONAL_USD = 500.0
MAX_HOLD_BARS = 180
MAX_ENTRY_BASIS = 0.02
MIN_ENTRY_BASIS = -0.0025
BASIS_STOP_WIDENING = 0.025
LANES = (
    {"lane_id": "AVG3_GT_1_5BP", "lookback": 3, "threshold": 0.00015},
    {"lane_id": "AVG6_GT_1_5BP", "lookback": 6, "threshold": 0.00015},
    {"lane_id": "AVG3_GT_3BP", "lookback": 3, "threshold": 0.00030},
    {"lane_id": "AVG6_GT_3BP", "lookback": 6, "threshold": 0.00030},
)


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def normalize_ts(value: Any) -> int:
    ts = int(float(value))
    while ts > 10**14:
        ts //= 1000
    return ts


def req_bytes(url: str, attempts: int = 4) -> bytes | None:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "TradingCore-FundingArchive/3.0", "Accept": "*/*"})
            with urlopen(request, timeout=40) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except Exception as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(min(6.0, 0.5 * 2**attempt))
    raise RuntimeError(f"download failed: {url}: {last}")


def req_json(url: str, attempts: int = 6) -> Any:
    raw = req_bytes(url, attempts)
    if raw is None:
        raise RuntimeError(f"not found: {url}")
    return json.loads(raw.decode("utf-8"))


def bounds() -> tuple[int, int]:
    now = int(time.time() * 1000)
    end = (now // BAR_MS) * BAR_MS - 1
    return end - int(YEARS * 365.25 * DAY_MS), end


def month_sequence(start_ms: int, end_ms: int) -> list[str]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    year, month = start.year, start.month
    out: list[str] = []
    while (year, month) <= (end.year, end.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return out


def unzip_rows(url: str) -> list[list[str]]:
    raw = req_bytes(url)
    if raw is None:
        return []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            return []
        text = archive.read(names[0]).decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def binance_archive_klines(futures: bool) -> list[dict[str, float | int]]:
    start, end = bounds()
    market = "futures/um" if futures else "spot"
    rows: dict[int, dict[str, float | int]] = {}
    for month in month_sequence(start, end):
        url = f"{BIN_ARCHIVE}/data/{market}/monthly/klines/{PAIR}/4h/{PAIR}-4h-{month}.zip"
        for item in unzip_rows(url):
            if not item or not str(item[0]).replace(".", "", 1).isdigit():
                continue
            try:
                ts = normalize_ts(item[0])
                if start <= ts <= end:
                    rows[ts] = {"ts": ts, "open": float(item[1]), "high": float(item[2]), "low": float(item[3]), "close": float(item[4])}
            except Exception:
                pass
    return [rows[key] for key in sorted(rows)]


def binance_archive_funding() -> list[tuple[int, float]]:
    start, end = bounds()
    values: dict[int, float] = {}
    for month in month_sequence(start, end):
        url = f"{BIN_ARCHIVE}/data/futures/um/monthly/fundingRate/{PAIR}/{PAIR}-fundingRate-{month}.zip"
        rows = unzip_rows(url)
        if not rows:
            continue
        header = [str(value).strip().lower() for value in rows[0]]
        has_header = any(any(ch.isalpha() for ch in value) for value in header)
        time_idx = 0
        rate_idx = 2 if len(header) > 2 else len(header) - 1
        if has_header:
            for index, value in enumerate(header):
                if value in {"calc_time", "fundingtime", "funding_time", "time", "timestamp"}:
                    time_idx = index
                if "funding" in value and "rate" in value:
                    rate_idx = index
            data_rows = rows[1:]
        else:
            data_rows = rows
        for item in data_rows:
            try:
                ts = normalize_ts(item[time_idx])
                rate = float(item[rate_idx])
                if start <= ts <= end and finite(rate):
                    values[ts] = rate
            except Exception:
                pass
    return sorted(values.items())


def okx_klines(swap: bool) -> list[dict[str, float | int]]:
    start, end = bounds()
    instrument = f"{SYMBOL}-USDT-SWAP" if swap else f"{SYMBOL}-USDT"
    cursor: int | None = None
    rows: dict[int, dict[str, float | int]] = {}
    for _ in range(260):
        params: dict[str, Any] = {"instId": instrument, "bar": "4H", "limit": 100}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = req_json(f"{OKX}/api/v5/market/history-candles?{urlencode(params)}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(payload)
        batch = payload.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                ts = int(item[0]); oldest = ts if oldest is None else min(oldest, ts)
                confirmed = str(item[8]) == "1" if len(item) > 8 else True
                if confirmed and start <= ts <= end:
                    rows[ts] = {"ts": ts, "open": float(item[1]), "high": float(item[2]), "low": float(item[3]), "close": float(item[4])}
            except Exception:
                pass
        if oldest is None or oldest <= start or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return [rows[key] for key in sorted(rows)]


def okx_funding() -> list[tuple[int, float]]:
    start, end = bounds()
    instrument = f"{SYMBOL}-USDT-SWAP"
    cursor: int | None = None
    values: dict[int, float] = {}
    for _ in range(80):
        params: dict[str, Any] = {"instId": instrument, "limit": 400}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = req_json(f"{OKX}/api/v5/public/funding-rate-history?{urlencode(params)}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(payload)
        batch = payload.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                ts = int(item["fundingTime"]); oldest = ts if oldest is None else min(oldest, ts)
                rate = float(item.get("realizedRate") or item.get("fundingRate"))
                if start <= ts <= end:
                    values[ts] = rate
            except Exception:
                pass
        if oldest is None or oldest <= start or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return sorted(values.items())


def align(spot: list[dict[str, Any]], perp: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spot_map = {int(row["ts"]): row for row in spot}; perp_map = {int(row["ts"]): row for row in perp}
    return [{"ts": ts, "spot": spot_map[ts], "perp": perp_map[ts], "basis": float(perp_map[ts]["close"]) / float(spot_map[ts]["close"]) - 1} for ts in sorted(set(spot_map) & set(perp_map))]


def performance(values: list[float]) -> dict[str, Any]:
    n = len(values); wins = [value for value in values if value > 0]; losses = [value for value in values if value < 0]
    gp, gl = sum(wins), -sum(losses); equity = peak = drawdown = 0.0
    for value in values:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    return {"closed_trades": n, "wins": len(wins), "losses": len(losses), "win_rate_percent": round(100 * len(wins) / n, 2) if n else None, "net_return_pct": round(100 * sum(values), 4) if n else None, "profit_factor": round(gp / gl, 4) if gl > 1e-12 else (99.0 if wins else None), "average_return_bps": round(10_000 * sum(values) / n, 4) if n else None, "max_drawdown_pct": round(100 * drawdown, 4) if n else None}


def simulate(lane: dict[str, Any], market: list[dict[str, Any]], funding: list[tuple[int, float]], start_ms: int, end_ms: int) -> dict[str, Any]:
    config = TradingCostConfig(); event_history: list[tuple[int, float]] = []; event_index = 0
    position: dict[str, Any] | None = None; pending_entry = False; pending_exit = False
    trade_returns: list[float] = []; trades: list[dict[str, Any]] = []; signals = 0

    def close_at(timestamp: int, spot_price: float, perp_price: float, reason: str) -> None:
        nonlocal position
        if position is None:
            return
        spot = compute_trade_costs(entry_price=position["spot_entry"], exit_price=spot_price, quantity=position["spot_qty"], side="LONG", config=config)
        perp = compute_trade_costs(entry_price=position["perp_entry"], exit_price=perp_price, quantity=position["perp_qty"], side="SHORT", config=config)
        pnl = float(spot["net_pnl"]) + float(perp["net_pnl"]) + float(position["funding_pnl"]); result = pnl / CAPITAL_USD
        trade_returns.append(result); trades.append({"entry_utc": datetime.fromtimestamp(position["entry_ts"] / 1000, tz=timezone.utc).isoformat(), "exit_utc": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat(), "funding_pnl": round(position["funding_pnl"], 6), "return_pct": round(100 * result, 5), "reason": reason}); position = None

    for index, row in enumerate(market):
        ts = int(row["ts"]); bar_close_ts = ts + BAR_MS - 1
        spot_open, perp_open = float(row["spot"]["open"]), float(row["perp"]["open"])
        if position is not None and ts > end_ms:
            close_at(ts, spot_open, perp_open, "WINDOW_END"); pending_exit = False; break
        if pending_exit and position is not None:
            close_at(ts, spot_open, perp_open, str(position.get("exit_reason") or "EXIT")); pending_exit = False
        if pending_entry and position is None and ts <= end_ms:
            position = {"entry_ts": ts, "entry_index": index, "spot_entry": spot_open, "perp_entry": perp_open, "spot_qty": LEG_NOTIONAL_USD / spot_open, "perp_qty": LEG_NOTIONAL_USD / perp_open, "entry_basis": perp_open / spot_open - 1, "funding_pnl": 0.0}; pending_entry = False
        while event_index < len(funding) and funding[event_index][0] <= bar_close_ts:
            event = funding[event_index]; event_history.append(event)
            if position is not None and event[0] > int(position["entry_ts"]):
                position["funding_pnl"] += LEG_NOTIONAL_USD * float(event[1])
            event_index += 1
        lookback = int(lane["lookback"]); recent_rates = [float(rate) for _, rate in event_history[-lookback:]]
        rolling = sum(recent_rates) / lookback if len(recent_rates) == lookback else None
        basis = float(row["perp"]["close"]) / float(row["spot"]["close"]) - 1
        if position is not None:
            holding = index - int(position["entry_index"])
            if holding >= MAX_HOLD_BARS: position["exit_reason"] = "MAX_HOLD"; pending_exit = True
            elif basis - float(position["entry_basis"]) >= BASIS_STOP_WIDENING: position["exit_reason"] = "BASIS_STOP"; pending_exit = True
            elif finite(rolling) and float(rolling) <= 0: position["exit_reason"] = "FUNDING_DECAY"; pending_exit = True
        if position is None and not pending_entry and not pending_exit and start_ms <= bar_close_ts <= end_ms:
            if finite(rolling) and float(rolling) >= float(lane["threshold"]) and MIN_ENTRY_BASIS <= basis <= MAX_ENTRY_BASIS and index + 1 < len(market):
                signals += 1; pending_entry = True
    if position is not None and market:
        last = market[-1]; close_at(int(last["ts"]) + BAR_MS - 1, float(last["spot"]["close"]), float(last["perp"]["close"]), "DATA_END")
    return {"signals": signals, "stats": performance(trade_returns), "recent_trades": trades[-15:]}


def checks_discovery(result: dict[str, Any], positive_segments: int) -> dict[str, bool]:
    stats = result["stats"]
    return {"trades": int(stats.get("closed_trades") or 0) >= 8, "profit_factor": finite(stats.get("profit_factor")) and float(stats["profit_factor"]) >= 1.15, "net_positive": finite(stats.get("net_return_pct")) and float(stats["net_return_pct"]) > 0, "drawdown": finite(stats.get("max_drawdown_pct")) and float(stats["max_drawdown_pct"]) <= 10, "segments": positive_segments >= 2}


def checks_holdout(result: dict[str, Any]) -> dict[str, bool]:
    stats = result["stats"]
    return {"trades": int(stats.get("closed_trades") or 0) >= 2, "profit_factor": finite(stats.get("profit_factor")) and float(stats["profit_factor"]) > 1, "net_positive": finite(stats.get("net_return_pct")) and float(stats["net_return_pct"]) > 0, "drawdown": finite(stats.get("max_drawdown_pct")) and float(stats["max_drawdown_pct"]) <= 5}


def rank(row: dict[str, Any]) -> tuple[float, int]:
    returns, counts = [], []
    for venue in ("BINANCE", "OKX"):
        stats = row["venues"][venue]["discovery"]["stats"]; returns.append(float(stats.get("net_return_pct") or -999)); counts.append(int(stats.get("closed_trades") or 0))
    return min(returns), min(counts)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_suffix(path.suffix + ".tmp"); tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"); tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--output-dir", default="funding_carry_runtime"); args = parser.parse_args(); safety = assert_safe_startup()
    market_data: dict[str, list[dict[str, Any]]] = {}; funding_data: dict[str, list[tuple[int, float]]] = {}; failures: dict[str, str] = {}
    loaders = {"BINANCE": (lambda: align(binance_archive_klines(False), binance_archive_klines(True)), binance_archive_funding), "OKX": (lambda: align(okx_klines(False), okx_klines(True)), okx_funding)}
    for venue, (market_loader, funding_loader) in loaders.items():
        try:
            market_data[venue] = market_loader(); funding_data[venue] = funding_loader(); print("FUNDING_V3_DATA", venue, "bars=", len(market_data[venue]), "funding=", len(funding_data[venue]), flush=True)
        except Exception as exc:
            failures[venue] = f"{type(exc).__name__}: {exc}"
    if len(market_data) < 2:
        report = {"schema": SCHEMA, "generated_at_utc": datetime.now(timezone.utc).isoformat(), "state": "FUNDING_CARRY_DATA_FAILURE", "data_failures": failures, "real_orders_enabled": False, "live_permission": False}; atomic_json(Path(args.output_dir) / "FUNDING_CARRY_RESULT.json", report); return 0
    common_start = max(rows[0]["ts"] for rows in market_data.values() if rows); common_end = min(rows[-1]["ts"] for rows in market_data.values() if rows)
    holdout_start = common_end - HOLDOUT_DAYS * DAY_MS; purge = PURGE_BARS * BAR_MS
    discovery = (max(common_start, holdout_start - DISCOVERY_DAYS * DAY_MS), holdout_start - purge - 1); holdout = (holdout_start + purge, common_end)
    segment_width = (discovery[1] - discovery[0] + 1) // 3; segments = [(discovery[0] + i * segment_width, discovery[0] + (i + 1) * segment_width - 1 if i < 2 else discovery[1]) for i in range(3)]
    rows_out: list[dict[str, Any]] = []
    for lane in LANES:
        venues: dict[str, Any] = {}; discovery_both = True
        for venue in ("BINANCE", "OKX"):
            full = simulate(lane, market_data[venue], funding_data[venue], *discovery); segs = [simulate(lane, market_data[venue], funding_data[venue], *window) for window in segments]
            positives = sum(1 for item in segs if float(item["stats"].get("net_return_pct") or 0) > 0); dchecks = checks_discovery(full, positives)
            hout = simulate(lane, market_data[venue], funding_data[venue], *holdout); hchecks = checks_holdout(hout)
            venues[venue] = {"discovery": full, "discovery_segments": segs, "positive_segments": positives, "discovery_checks": dchecks, "discovery_passed": all(dchecks.values()), "holdout": hout, "holdout_checks": hchecks, "holdout_passed": all(hchecks.values())}; discovery_both = discovery_both and all(dchecks.values())
        rows_out.append({"symbol": SYMBOL, **lane, "venues": venues, "discovery_passed_both_venues": discovery_both})
    candidates = [row for row in rows_out if row["discovery_passed_both_venues"]]; candidates.sort(key=rank, reverse=True); selected = candidates[0] if candidates else None
    holdout_both = bool(selected and all(selected["venues"][venue]["holdout_passed"] for venue in ("BINANCE", "OKX")))
    state = "FUNDING_CARRY_CANDIDATE_FOUND_FORWARD_REQUIRED" if holdout_both else ("FUNDING_CARRY_DISCOVERY_FAILED_HOLDOUT" if selected else "NO_FUNDING_CARRY_CANDIDATE")
    report = {"schema": SCHEMA, "generated_at_utc": datetime.now(timezone.utc).isoformat(), "state": state, "symbol": SYMBOL, "history_years": YEARS, "bar": "4H", "selected": {"symbol": selected["symbol"], "lane_id": selected["lane_id"]} if selected else None, "selected_holdout_passed_both_venues": holdout_both, "discovery_candidates": [row["lane_id"] for row in candidates], "results": rows_out, "data_failures": failures, "execution_model": "official Binance ZIP archives + OKX 4H; next-bar entry; equal spot/perp notionals; conservative two-leg costs", "safety": safety, "private_api_used": False, "real_orders_enabled": False, "live_permission": False, "note": "Historical discovery/holdout evidence only. Any pass still requires fresh forward execution and exchange/custody risk review."}
    atomic_json(Path(args.output_dir) / "FUNDING_CARRY_RESULT.json", report); print("FUNDING_V3", state, "selected=", report["selected"], "holdout_both=", holdout_both, "failures=", failures, "LIVE=False"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
