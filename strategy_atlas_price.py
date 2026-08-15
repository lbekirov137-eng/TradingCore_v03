#!/usr/bin/env python3
"""Parallel historical replay for Strategy Atlas price/niche mechanisms.

Tests many structurally different long-only spot mechanisms across Binance and
OKX using fixed rules, conservative TradingCore costs, chronological halves and
no final-data tuning. Research/PAPER only; no authenticated API or orders.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_engine.strategies.contracts import Candle
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from config.startup_safety import assert_safe_startup
from strategy_atlas_protocol import (
    MAX_FULL_DD_R,
    MIN_FULL_EXPECTANCY_R,
    MIN_FULL_PF,
    MIN_HALF_EXPECTANCY_R,
    MIN_HALF_PF,
    MIN_SEGMENT_ROBUSTNESS,
    MIN_TRADES_PER_HALF,
    MIN_TRADES_PER_VENUE,
    NICHE_FAMILIES,
    PRICE_FAMILIES,
    PROTOCOL_FINGERPRINT,
    PROTOCOL_VERSION,
    REFERENCE_CAPITAL_USD,
    RISK_AMOUNT_USD,
    SPOT_HISTORY_DAYS,
    SPOT_SYMBOLS,
    SPOT_TIMEFRAMES,
)

BINANCE_BASE = "https://data-api.binance.vision"
OKX_BASE = "https://www.okx.com"
DAY_MS = 86_400_000
SCHEMA = "TRADINGCORE_STRATEGY_ATLAS_PRICE_V1"
FAMILIES = PRICE_FAMILIES + tuple(
    x for x in NICHE_FAMILIES if x not in {"CORRELATION_BREAK_REVERSAL", "CORRELATION_BREAK_MOMENTUM"}
)


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
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "TradingCore-StrategyAtlas/1.0"})
            with urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay = min(8.0, delay * 2.0)
    raise RuntimeError(f"public market request failed: {last}")


def timeframe_meta(timeframe: str) -> tuple[int, str]:
    if timeframe == "1h":
        return 3_600_000, "1H"
    if timeframe == "4h":
        return 14_400_000, "4H"
    raise ValueError(timeframe)


def closed_window(timeframe: str) -> tuple[int, int]:
    interval_ms, _ = timeframe_meta(timeframe)
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // interval_ms) * interval_ms - 1
    start_ms = end_ms - SPOT_HISTORY_DAYS * DAY_MS
    return start_ms, end_ms


def fetch_binance(symbol: str, timeframe: str) -> list[Candle]:
    interval_ms, _ = timeframe_meta(timeframe)
    start_ms, end_ms = closed_window(timeframe)
    cursor = start_ms
    by: dict[int, Candle] = {}
    while cursor <= end_ms:
        q = urlencode({"symbol": symbol, "interval": timeframe, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        payload = request_json(f"{BINANCE_BASE}/api/v3/klines?{q}")
        if not isinstance(payload, list) or not payload:
            break
        last_open = None
        for r in payload:
            if not isinstance(r, list) or len(r) < 7:
                continue
            try:
                ts = int(r[0]); close_ts = int(r[6])
                if ts < start_ms or close_ts > end_ms:
                    continue
                by[ts] = Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                last_open = ts
            except (TypeError, ValueError):
                continue
        if last_open is None or len(payload) < 1000:
            break
        nxt = last_open + interval_ms
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.02)
    return sorted(by.values(), key=lambda x: x.open_time_ms)


def fetch_okx(symbol: str, timeframe: str) -> list[Candle]:
    interval_ms, bar = timeframe_meta(timeframe)
    start_ms, end_ms = closed_window(timeframe)
    inst = f"{symbol[:-4]}-USDT"
    cursor: int | None = None
    by: dict[int, Candle] = {}
    for _ in range(160):
        params: dict[str, Any] = {"instId": inst, "bar": bar, "limit": 100}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = request_json(f"{OKX_BASE}/api/v5/market/history-candles?{urlencode(params)}")
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX error {inst} {timeframe}: {payload}")
        rows = payload.get("data") or []
        if not rows:
            break
        oldest = None
        for r in rows:
            if not isinstance(r, list) or len(r) < 6:
                continue
            try:
                ts = int(r[0])
                confirmed = str(r[8]) == "1" if len(r) > 8 else True
                if not confirmed:
                    continue
                if ts < start_ms or ts > end_ms:
                    oldest = ts if oldest is None else min(oldest, ts)
                    continue
                by[ts] = Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                oldest = ts if oldest is None else min(oldest, ts)
            except (TypeError, ValueError):
                continue
        if oldest is None or oldest <= start_ms:
            break
        if cursor is not None and oldest >= cursor:
            break
        cursor = oldest
        time.sleep(0.23)
    return sorted(by.values(), key=lambda x: x.open_time_ms)


def ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    cur = sum(values[:period]) / period
    out[period - 1] = cur
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        cur = alpha * values[i] + (1.0 - alpha) * cur
        out[i] = cur
    return out


def atr_series(rows: list[Candle], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(rows)
    trs: list[float] = [0.0] * len(rows)
    for i in range(1, len(rows)):
        c, p = rows[i], rows[i - 1]
        trs[i] = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
    if len(rows) <= period:
        return out
    window = sum(trs[1:period + 1])
    out[period] = window / period
    for i in range(period + 1, len(rows)):
        window += trs[i] - trs[i - period]
        out[i] = window / period
    return out


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [0.0] * len(values); losses = [0.0] * len(values)
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains[i] = max(d, 0.0); losses[i] = max(-d, 0.0)
    sg = sum(gains[1:period + 1]); sl = sum(losses[1:period + 1])
    for i in range(period, len(values)):
        if i > period:
            sg += gains[i] - gains[i - period]
            sl += losses[i] - losses[i - period]
        avg_g = sg / period; avg_l = sl / period
        out[i] = 100.0 if avg_l <= 1e-12 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def precompute(rows: list[Candle], timeframe: str) -> dict[str, Any]:
    closes = [x.close for x in rows]
    volumes = [x.volume for x in rows]
    e20 = ema_series(closes, 20); e50 = ema_series(closes, 50); e200 = ema_series(closes, 200)
    atr = atr_series(rows, 14); rsi = rsi_series(closes, 14)
    hours = 1 if timeframe == "1h" else 4
    b24 = max(1, 24 // hours); b7d = max(2, 168 // hours)
    feat: list[dict[str, Any]] = []
    for i, c in enumerate(rows):
        d: dict[str, Any] = {"ema20": e20[i], "ema50": e50[i], "ema200": e200[i], "atr": atr[i], "rsi": rsi[i]}
        d["ret24"] = (c.close / rows[i - b24].close - 1.0) if i >= b24 else None
        d["ret7d"] = (c.close / rows[i - b7d].close - 1.0) if i >= b7d else None
        if i >= 20:
            prev20 = rows[i - 20:i]
            vals = closes[i - 20:i]
            d["high20"] = max(x.high for x in prev20); d["low20"] = min(x.low for x in prev20)
            mean = statistics.fmean(vals); sd = statistics.pstdev(vals)
            d["sma20"] = mean; d["bb_low"] = mean - 2.0 * sd; d["bb_high"] = mean + 2.0 * sd
            d["vol20"] = statistics.fmean(volumes[i - 20:i])
        else:
            d.update({"high20": None, "low20": None, "sma20": None, "bb_low": None, "bb_high": None, "vol20": None})
        if i >= 48:
            d["high48"] = max(x.high for x in rows[i - 48:i]); d["low48"] = min(x.low for x in rows[i - 48:i])
        else:
            d["high48"] = d["low48"] = None
        if i >= 100 and atr[i] is not None:
            hist = [x for x in atr[i - 100:i] if isinstance(x, (int, float)) and x > 0]
            d["atr_rank"] = sum(1 for x in hist if x <= float(atr[i])) / len(hist) if hist else None
        else:
            d["atr_rank"] = None
        feat.append(d)
    return {"features": feat, "hours": hours}


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def signal(family: str, rows: list[Candle], f: list[dict[str, Any]], i: int) -> bool:
    if i < 201:
        return False
    c = rows[i]; p = rows[i - 1]; x = f[i]; y = f[i - 1]
    e20, e50, e200, a, rsi = x["ema20"], x["ema50"], x["ema200"], x["atr"], x["rsi"]
    if not all(finite(v) for v in (e20, e50, e200, a, rsi)):
        return False
    trend = float(e50) > float(e200)
    gap = abs(float(e20) - float(e50)) / c.close
    bullish = c.close > c.open
    if family == "TREND_DONCHIAN_BREAKOUT":
        return trend and finite(x["high48"]) and c.close > float(x["high48"]) and bullish
    if family == "TREND_EMA_PULLBACK":
        return trend and c.low <= float(e20) and c.close > float(e20) and bullish
    if family == "TIME_SERIES_MOMENTUM":
        return trend and finite(x["ret7d"]) and float(x["ret7d"]) >= 0.04 and c.close > p.close
    if family == "VOLUME_CONFIRMED_BREAKOUT":
        return trend and finite(x["high20"]) and finite(x["vol20"]) and c.close > float(x["high20"]) and c.volume >= 1.5 * float(x["vol20"])
    if family == "VOL_COMPRESSION_BREAKOUT":
        return trend and finite(x["atr_rank"]) and float(x["atr_rank"]) <= 0.25 and finite(x["high20"]) and c.close > float(x["high20"])
    if family == "RSI_PANIC_MEAN_REVERSION":
        return float(rsi) <= 25.0 and finite(x["ret24"]) and float(x["ret24"]) <= -0.04 and bullish
    if family == "BOLLINGER_REENTRY_MEAN_REVERSION":
        return finite(y["bb_low"]) and finite(x["bb_low"]) and p.close < float(y["bb_low"]) and c.close > float(x["bb_low"]) and bullish
    if family == "RANGE_FADE":
        return gap <= 0.004 and finite(x["bb_low"]) and c.low < float(x["bb_low"]) and c.close > float(x["bb_low"]) and bullish
    if family == "OPENING_RANGE_BREAKOUT_UTC":
        dt = datetime.fromtimestamp(c.open_time_ms / 1000.0, tz=timezone.utc)
        day_start = c.open_time_ms - (dt.hour * 60 + dt.minute) * 60_000
        day_rows = [z for z in rows[max(0, i - 24):i] if z.open_time_ms >= day_start]
        need = 4 if f[i]["hours"] if False else 4
        if len(day_rows) < 4:
            return False
        opening = day_rows[:4]
        return trend and dt.hour >= 4 and c.close > max(z.high for z in opening) and bullish
    if family == "SESSION_MOMENTUM_ASIA_EU_US":
        hour = datetime.fromtimestamp(c.open_time_ms / 1000.0, tz=timezone.utc).hour
        ret24 = float(x["ret24"]) if finite(x["ret24"]) else 0.0
        return trend and hour in {0, 8, 12, 13, 16} and ret24 >= 0.015 and bullish
    if family == "WEEKEND_WEEKDAY_ROTATION":
        dt = datetime.fromtimestamp(c.open_time_ms / 1000.0, tz=timezone.utc)
        return trend and dt.weekday() in {0, 4} and dt.hour == 0 and finite(x["ret7d"]) and float(x["ret7d"]) > 0
    if family == "VOLATILITY_REGIME_SWITCH":
        rank = float(x["atr_rank"]) if finite(x["atr_rank"]) else 0.5
        breakout = trend and rank >= 0.60 and finite(x["high20"]) and c.close > float(x["high20"])
        fade = gap <= 0.004 and rank <= 0.35 and finite(x["bb_low"]) and c.low < float(x["bb_low"]) and bullish
        return breakout or fade
    if family == "GRID_RANGE_CAPTURE":
        return gap <= 0.003 and float(rsi) <= 40 and finite(x["sma20"]) and c.close < float(x["sma20"]) and bullish
    if family == "VOLATILITY_BREAKOUT_AFTER_DEAD_ZONE":
        return trend and finite(x["atr_rank"]) and float(x["atr_rank"]) <= 0.20 and finite(x["high48"]) and c.close > float(x["high48"])
    if family == "FAILED_BREAKOUT_FADE":
        return finite(y["low20"]) and p.low < float(y["low20"]) and p.close > float(y["low20"]) and bullish and c.close > p.close
    if family == "LIQUIDITY_SWEEP_RECLAIM_PROXY":
        return finite(x["low20"]) and c.low < float(x["low20"]) and c.close > float(x["low20"]) and bullish
    if family == "DAY_OF_WEEK_EFFECT":
        dt = datetime.fromtimestamp(c.open_time_ms / 1000.0, tz=timezone.utc)
        return trend and dt.weekday() == 0 and dt.hour == 0
    if family == "UTC_TIME_BUCKET_EFFECT":
        hour = datetime.fromtimestamp(c.open_time_ms / 1000.0, tz=timezone.utc).hour
        return trend and hour in {0, 8, 16} and bullish and c.close > float(e20)
    return False


def segment_robustness(trades: list[ClosedTrade], segments: int = 4) -> float | None:
    if len(trades) < segments * 2:
        return None
    step = max(1, len(trades) // segments); good = 0; used = 0
    for k in range(segments):
        block = trades[k * step:(k + 1) * step if k < segments - 1 else len(trades)]
        if not block:
            continue
        used += 1
        s = build_stats(block)
        if finite(s.get("net_pnl")) and float(s["net_pnl"]) > 0:
            good += 1
    return round(good / used, 4) if used else None


def simulate(family: str, symbol: str, timeframe: str, rows: list[Candle]) -> dict[str, Any]:
    pc = precompute(rows, timeframe); feat = pc["features"]; hours = pc["hours"]
    cost = TradingCostConfig(); trades: list[ClosedTrade] = []; position = None; signals = 0
    max_bars = max(2, 24 // hours)
    for i, c in enumerate(rows):
        if position is not None:
            exit_px = None
            if c.low <= position["stop"]:
                exit_px = position["stop"]
            elif c.high >= position["target"]:
                exit_px = position["target"]
            elif i - position["entry_i"] >= max_bars:
                exit_px = c.close
            if exit_px is not None:
                costs = compute_trade_costs(entry_price=position["entry"], exit_price=exit_px, quantity=position["qty"], side="LONG", config=cost)
                risk = position["risk"]
                r = costs["net_pnl"] / risk if risk > 0 else None
                trades.append(ClosedTrade(f"{family}:{symbol}:{timeframe}", datetime.fromtimestamp((c.open_time_ms + timeframe_meta(timeframe)[0]) / 1000.0, tz=timezone.utc).isoformat(), symbol, float(costs["net_pnl"]), r))
                position = None
        if position is not None:
            continue
        if not signal(family, rows, feat, i):
            continue
        a = feat[i].get("atr")
        if not finite(a) or float(a) <= 0:
            continue
        signals += 1
        entry = c.close; stop = entry - 1.5 * float(a); target = entry + 3.0 * float(a)
        if stop <= 0:
            continue
        risk_per_unit = entry - stop
        qty = min(RISK_AMOUNT_USD / risk_per_unit, REFERENCE_CAPITAL_USD / entry)
        risk = qty * risk_per_unit
        if qty <= 0 or risk <= 0:
            continue
        position = {"entry": entry, "stop": stop, "target": target, "qty": qty, "risk": risk, "entry_i": i}
    return {"family": family, "symbol": symbol, "timeframe": timeframe, "signals": signals, "trades": trades, "stats": build_stats(trades), "segment_robustness": segment_robustness(trades)}


def num(stats: dict[str, Any], key: str, fallback: float = -1e99) -> float:
    v = stats.get(key)
    return float(v) if finite(v) else fallback


def evaluate_lane(lane: dict[str, Any], midpoint: str) -> dict[str, Any]:
    trades: list[ClosedTrade] = lane["trades"]
    first = [t for t in trades if str(t.closed_at_utc) < midpoint]; second = [t for t in trades if str(t.closed_at_utc) >= midpoint]
    full = build_stats(trades); a = build_stats(first); b = build_stats(second); robust = segment_robustness(trades)
    checks = {
        "full_trades": int(full.get("closed_trades") or 0) >= MIN_TRADES_PER_VENUE,
        "full_pf": num(full, "profit_factor") >= MIN_FULL_PF,
        "full_expectancy": num(full, "expectancy_r") >= MIN_FULL_EXPECTANCY_R,
        "full_net": num(full, "net_pnl") > 0,
        "full_dd": 0 <= num(full, "max_drawdown_r", 1e99) <= MAX_FULL_DD_R,
        "robustness": finite(robust) and float(robust) >= MIN_SEGMENT_ROBUSTNESS,
        "first_trades": int(a.get("closed_trades") or 0) >= MIN_TRADES_PER_HALF,
        "first_pf": num(a, "profit_factor") >= MIN_HALF_PF,
        "first_exp": num(a, "expectancy_r") > MIN_HALF_EXPECTANCY_R,
        "second_trades": int(b.get("closed_trades") or 0) >= MIN_TRADES_PER_HALF,
        "second_pf": num(b, "profit_factor") >= MIN_HALF_PF,
        "second_exp": num(b, "expectancy_r") > MIN_HALF_EXPECTANCY_R,
    }
    return {"full": full, "first_half": a, "second_half": b, "segment_robustness": robust, "checks": checks, "passed": all(checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", default="strategy_atlas_price_runtime"); args = parser.parse_args()
    safety = assert_safe_startup(); out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    market: dict[tuple[str, str, str], list[Candle]] = {}; failures: dict[str, str] = {}
    jobs = [(v, s, tf) for v in ("BINANCE", "OKX") for s in SPOT_SYMBOLS for tf in SPOT_TIMEFRAMES]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fm = {}
        for v, s, tf in jobs:
            fn = fetch_binance if v == "BINANCE" else fetch_okx
            fm[pool.submit(fn, s, tf)] = (v, s, tf)
        for future in as_completed(fm):
            key = fm[future]; label = ":".join(key)
            try:
                rows = future.result(); market[key] = rows; print(f"ATLAS_DATA {label} bars={len(rows)}", flush=True)
            except Exception as exc:
                failures[label] = f"{type(exc).__name__}: {exc}"; print(f"ATLAS_DATA_FAIL {label} {failures[label]}", flush=True)
    results = []
    for family in FAMILIES:
        for symbol in SPOT_SYMBOLS:
            for timeframe in SPOT_TIMEFRAMES:
                start_ms, end_ms = closed_window(timeframe); mid = datetime.fromtimestamp((start_ms + (end_ms - start_ms)//2)/1000.0, tz=timezone.utc).isoformat()
                venues = {}; all_pass = True
                for venue in ("BINANCE", "OKX"):
                    rows = market.get((venue, symbol, timeframe)) or []
                    if len(rows) < 500:
                        venues[venue] = {"passed": False, "error": "INSUFFICIENT_DATA"}; all_pass = False; continue
                    lane = simulate(family, symbol, timeframe, rows); ev = evaluate_lane(lane, mid); venues[venue] = ev; all_pass = all_pass and ev["passed"]
                state = "ATLAS_PRICE_PASS_NOT_LIVE" if all_pass else "ATLAS_PRICE_REJECT"
                results.append({"family": family, "symbol": symbol, "timeframe": timeframe, "state": state, "venues": venues})
                print(f"ATLAS_PRICE {family}:{symbol}:{timeframe} {state}", flush=True)
    passed = [x for x in results if x["state"] == "ATLAS_PRICE_PASS_NOT_LIVE"]
    def rank(item: dict[str, Any]) -> tuple:
        pfs=[]; exps=[]; n=0
        for v in ("BINANCE", "OKX"):
            full=((item.get("venues") or {}).get(v) or {}).get("full") or {}
            pfs.append(num(full,"profit_factor")); exps.append(num(full,"expectancy_r")); n += int(full.get("closed_trades") or 0)
        return (min(pfs), min(exps), n)
    passed.sort(key=rank, reverse=True)
    report = {
        "schema": SCHEMA, "generated_at_utc": now_utc(), "state": "ATLAS_PRICE_CANDIDATE_FOUND_NOT_LIVE" if passed else "NO_ATLAS_PRICE_CANDIDATE",
        "protocol_version": PROTOCOL_VERSION, "protocol_fingerprint": PROTOCOL_FINGERPRINT, "families_tested": list(FAMILIES),
        "lane_count": len(results), "passing_lane_count": len(passed), "candidate": passed[0] if passed else None,
        "passing_lanes": [{"family":x["family"],"symbol":x["symbol"],"timeframe":x["timeframe"]} for x in passed], "data_failures": failures, "lanes": results,
        "safety": safety, "private_api_used": False, "real_orders_enabled": False, "live_permission": False,
    }
    atomic_json(out / "STRATEGY_ATLAS_PRICE_RESULT.json", report)
    print("="*96); print("STRATEGY ATLAS PRICE FINAL RESULT"); print("State:", report["state"]); print("Passing:", len(passed), "/", len(results)); print("Candidate:", report["candidate"] and (report["candidate"]["family"],report["candidate"]["symbol"],report["candidate"]["timeframe"])); print("LIVE / real orders: DISABLED"); print("="*96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
