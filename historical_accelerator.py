#!/usr/bin/env python3
"""TradingCore Historical Accelerator V1.

Purpose: obtain a fast yes/no answer from a large PUBLIC historical sample
instead of waiting for live liquidation collectors. This module does not send
orders and has no authenticated exchange path.

Data (Bybit V5 public market API):
- 1h spot klines for execution/price features
- funding-rate history
- 4h open-interest history
- 4h long/short account ratio history

The universe is read from Collector C's frozen UNIVERSE_LOCK when available.
The research protocol and thresholds live only in historical_accelerator_protocol.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from api.strategy_supervisor.validation import validate_candidate
from config.startup_safety import assert_safe_startup
import historical_accelerator_protocol as protocol

REST = "https://api.bybit.com"
SCHEMA = "TRADINGCORE_HISTORICAL_ACCELERATOR_V1"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass(frozen=True)
class Sample:
    ts: int
    value: float


@dataclass
class TradeRecord:
    family_id: str
    symbol: str
    signal_ms: int
    entry_ms: int
    closed_ms: int
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str
    quantity: float
    net_pnl: float
    r_multiple: float
    features: dict[str, Any]

    @property
    def closed_at_utc(self) -> str:
        return utc(self.closed_ms)

    def closed_trade(self) -> ClosedTrade:
        return ClosedTrade(
            strategy_id=self.family_id,
            closed_at_utc=self.closed_at_utc,
            regime=self.symbol,
            net_pnl=self.net_pnl,
            r_multiple=self.r_multiple,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id,
            "symbol": self.symbol,
            "signal_utc": utc(self.signal_ms),
            "entry_utc": utc(self.entry_ms),
            "closed_utc": self.closed_at_utc,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "exit": self.exit,
            "exit_reason": self.exit_reason,
            "quantity": self.quantity,
            "net_pnl": self.net_pnl,
            "r_multiple": self.r_multiple,
            "features": self.features,
            "real_order_sent": False,
        }


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _throttle() -> None:
    global _LAST_REQUEST
    with _REQUEST_LOCK:
        now = time.monotonic()
        wait = 0.055 - (now - _LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST = time.monotonic()


def http_json(path: str, params: dict[str, Any], attempts: int = 7) -> dict[str, Any]:
    query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{REST}{path}?{query}"
    last: Exception | None = None
    for attempt in range(attempts):
        _throttle()
        try:
            req = Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "TradingCore-HistoricalAccelerator/1.0"},
                method="GET",
            )
            with urlopen(req, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("Bybit response is not an object")
            code = int(payload.get("retCode", -1))
            if code != 0:
                raise RuntimeError(f"Bybit retCode={code} retMsg={payload.get('retMsg')}")
            return payload
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(min(8.0, 0.5 * (2 ** attempt)))
    raise RuntimeError(f"Public Bybit request failed after retries: {url}: {last}")


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[Bar]:
    rows_by_ts: dict[int, Bar] = {}
    cursor_end = end_ms
    for _ in range(100):
        payload = http_json(
            "/v5/market/kline",
            {"category": "spot", "symbol": symbol, "interval": protocol.BAR_INTERVAL,
             "start": start_ms, "end": cursor_end, "limit": 1000},
        )
        rows = ((payload.get("result") or {}).get("list") or [])
        if not rows:
            break
        oldest: int | None = None
        for raw in rows:
            if not isinstance(raw, list) or len(raw) < 7:
                continue
            try:
                bar = Bar(
                    ts=int(raw[0]), open=float(raw[1]), high=float(raw[2]), low=float(raw[3]),
                    close=float(raw[4]), volume=float(raw[5]), turnover=float(raw[6]),
                )
            except (TypeError, ValueError):
                continue
            if start_ms <= bar.ts <= end_ms:
                rows_by_ts[bar.ts] = bar
                oldest = bar.ts if oldest is None else min(oldest, bar.ts)
        if oldest is None or oldest <= start_ms:
            break
        if oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        if len(rows) < 1000:
            break
    return sorted(rows_by_ts.values(), key=lambda x: x.ts)


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[Sample]:
    by_ts: dict[int, Sample] = {}
    cursor_end = end_ms
    for _ in range(100):
        payload = http_json(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "endTime": cursor_end, "limit": 200},
        )
        rows = ((payload.get("result") or {}).get("list") or [])
        if not rows:
            break
        oldest: int | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row["fundingRateTimestamp"])
                value = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if start_ms <= ts <= end_ms and math.isfinite(value):
                by_ts[ts] = Sample(ts, value)
        if oldest is None or oldest <= start_ms or oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        if len(rows) < 200:
            break
    return sorted(by_ts.values(), key=lambda x: x.ts)


def fetch_cursor_series(
    path: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    interval_key: str,
    interval_value: str,
    value_key: str,
    limit: int,
) -> list[Sample]:
    by_ts: dict[int, Sample] = {}
    cursor = ""
    seen_cursors: set[str] = set()
    for _ in range(200):
        params: dict[str, Any] = {
            "category": "linear", "symbol": symbol,
            interval_key: interval_value,
            "startTime": start_ms, "endTime": end_ms, "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        payload = http_json(path, params)
        result = payload.get("result") or {}
        rows = result.get("list") if isinstance(result, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row["timestamp"])
                value = float(row[value_key])
            except (KeyError, TypeError, ValueError):
                continue
            if start_ms <= ts <= end_ms and math.isfinite(value):
                by_ts[ts] = Sample(ts, value)
        next_cursor = str(result.get("nextPageCursor") or "") if isinstance(result, dict) else ""
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return sorted(by_ts.values(), key=lambda x: x.ts)


def fetch_oi(symbol: str, start_ms: int, end_ms: int) -> list[Sample]:
    return fetch_cursor_series(
        "/v5/market/open-interest", symbol, start_ms, end_ms,
        interval_key="intervalTime", interval_value="4h", value_key="openInterest", limit=200,
    )


def fetch_ratio(symbol: str, start_ms: int, end_ms: int) -> list[Sample]:
    return fetch_cursor_series(
        "/v5/market/account-ratio", symbol, start_ms, end_ms,
        interval_key="period", interval_value="4h", value_key="buyRatio", limit=500,
    )


def cache_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def cache_read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def serialise_bars(rows: list[Bar]) -> list[list[float | int]]:
    return [[x.ts, x.open, x.high, x.low, x.close, x.volume, x.turnover] for x in rows]


def deserialise_bars(rows: list[Any]) -> list[Bar]:
    out: list[Bar] = []
    for r in rows:
        try:
            out.append(Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6])))
        except (TypeError, ValueError, IndexError):
            pass
    return sorted(out, key=lambda x: x.ts)


def serialise_samples(rows: list[Sample]) -> list[list[float | int]]:
    return [[x.ts, x.value] for x in rows]


def deserialise_samples(rows: list[Any]) -> list[Sample]:
    out: list[Sample] = []
    for r in rows:
        try:
            out.append(Sample(int(r[0]), float(r[1])))
        except (TypeError, ValueError, IndexError):
            pass
    return sorted(out, key=lambda x: x.ts)


def load_symbol(symbol: str, sample_lock: dict[str, Any], cache_root: Path) -> dict[str, Any]:
    start_ms = int(sample_lock["start_ms"])
    end_ms = int(sample_lock["end_ms"])
    path = cache_root / f"{symbol}.json.gz"
    cached = cache_read(path)
    if cached and cached.get("protocol_fingerprint") == protocol.PROTOCOL_FINGERPRINT \
            and cached.get("start_ms") == start_ms and cached.get("end_ms") == end_ms:
        return {
            "symbol": symbol,
            "bars": deserialise_bars(cached.get("bars") or []),
            "funding": deserialise_samples(cached.get("funding") or []),
            "oi": deserialise_samples(cached.get("oi") or []),
            "ratio": deserialise_samples(cached.get("ratio") or []),
            "from_cache": True,
        }

    bars = fetch_klines(symbol, start_ms, end_ms)
    funding = fetch_funding(symbol, start_ms, end_ms)
    oi = fetch_oi(symbol, start_ms, end_ms)
    ratio = fetch_ratio(symbol, start_ms, end_ms)
    payload = {
        "schema": "TRADINGCORE_HISTORICAL_ACCELERATOR_CACHE_V1",
        "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
        "symbol": symbol,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "bars": serialise_bars(bars),
        "funding": serialise_samples(funding),
        "oi": serialise_samples(oi),
        "ratio": serialise_samples(ratio),
    }
    cache_write(path, payload)
    return {"symbol": symbol, "bars": bars, "funding": funding, "oi": oi, "ratio": ratio, "from_cache": False}


def at_or_before(rows: list[Sample], ts: int) -> tuple[int, float] | None:
    if not rows:
        return None
    times = [r.ts for r in rows]
    idx = bisect.bisect_right(times, ts) - 1
    if idx < 0:
        return None
    return idx, rows[idx].value


def funding_features(rows: list[Sample], ts: int) -> tuple[float | None, float | None]:
    hit = at_or_before(rows, ts)
    if hit is None:
        return None, None
    idx, value = hit
    if idx + 1 < protocol.MIN_FUNDING_HISTORY:
        return value, None
    start = max(0, idx + 1 - protocol.FUNDING_Z_LOOKBACK)
    hist = [r.value for r in rows[start:idx + 1]]
    if len(hist) < protocol.MIN_FUNDING_HISTORY:
        return value, None
    mean = statistics.fmean(hist)
    sd = statistics.pstdev(hist)
    if not math.isfinite(sd) or sd <= 1e-12:
        return value, 0.0
    return value, (value - mean) / sd


def pct_change_series(rows: list[Sample], ts: int, hours: int) -> float | None:
    now_hit = at_or_before(rows, ts)
    old_hit = at_or_before(rows, ts - hours * HOUR_MS)
    if now_hit is None or old_hit is None:
        return None
    now_value = now_hit[1]
    old_value = old_hit[1]
    if old_value <= 0:
        return None
    return now_value / old_value - 1.0


def atr14(bars: list[Bar], index: int) -> float | None:
    n = protocol.ATR_PERIOD
    if index < n:
        return None
    rows = bars[index - n:index + 1]
    if len(rows) != n + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(rows)):
        c, p = rows[i], rows[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(trs) / len(trs) if len(trs) == n else None


def family_signal(family: dict[str, Any], features: dict[str, Any]) -> bool:
    def le(key: str, feature: str) -> bool:
        if key not in family:
            return True
        value = features.get(feature)
        return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) <= float(family[key])

    if not le("funding_z_lte", "funding_z"):
        return False
    if not le("funding_lte", "funding"):
        return False
    if not le("return_8h_lte", "return_8h"):
        return False
    if not le("return_4h_lte", "return_4h"):
        return False
    if not le("oi_change_8h_lte", "oi_change_8h"):
        return False
    if not le("oi_change_4h_lte", "oi_change_4h"):
        return False
    if not le("buy_ratio_lte", "buy_ratio"):
        return False
    if family.get("require_bullish_signal_candle") and not features.get("bullish_signal_candle"):
        return False
    return True


def simulate_symbol(symbol_data: dict[str, Any], family: dict[str, Any]) -> list[TradeRecord]:
    symbol = symbol_data["symbol"]
    bars: list[Bar] = symbol_data["bars"]
    funding: list[Sample] = symbol_data["funding"]
    oi: list[Sample] = symbol_data["oi"]
    ratio: list[Sample] = symbol_data["ratio"]
    if len(bars) < 100:
        return []

    costs = TradingCostConfig()
    records: list[TradeRecord] = []
    busy_until_ms = 0

    for i in range(max(protocol.ATR_PERIOD, 8), len(bars) - 1):
        bar = bars[i]
        # Require contiguous hourly price history for the return features.
        if bars[i - 8].ts != bar.ts - 8 * HOUR_MS or bars[i - 4].ts != bar.ts - 4 * HOUR_MS:
            continue
        signal_ms = bar.ts + HOUR_MS - 1
        if signal_ms < busy_until_ms:
            continue
        f_rate, f_z = funding_features(funding, signal_ms)
        oi4 = pct_change_series(oi, signal_ms, 4)
        oi8 = pct_change_series(oi, signal_ms, 8)
        ratio_hit = at_or_before(ratio, signal_ms)
        buy_ratio = ratio_hit[1] if ratio_hit else None
        ret4 = bar.close / bars[i - 4].close - 1.0 if bars[i - 4].close > 0 else None
        ret8 = bar.close / bars[i - 8].close - 1.0 if bars[i - 8].close > 0 else None
        features = {
            "funding": f_rate,
            "funding_z": f_z,
            "return_4h": ret4,
            "return_8h": ret8,
            "oi_change_4h": oi4,
            "oi_change_8h": oi8,
            "buy_ratio": buy_ratio,
            "bullish_signal_candle": bar.close > bar.open,
        }
        if not family_signal(family, features):
            continue

        a = atr14(bars, i)
        if a is None or a <= 0:
            continue
        entry_i = i + 1
        entry_bar = bars[entry_i]
        if entry_bar.ts != bar.ts + HOUR_MS:
            continue
        entry = entry_bar.open
        stop = entry - protocol.ATR_STOP_MULTIPLE * a
        if entry <= 0 or stop <= 0 or stop >= entry:
            continue
        risk_unit = entry - stop
        quantity = protocol.RISK_AMOUNT_USD / risk_unit
        position_notional = quantity * entry
        if position_notional > protocol.REFERENCE_CAPITAL_USD * protocol.MAX_LEVERAGE + 1e-9:
            continue
        target = entry + protocol.RISK_REWARD * risk_unit

        exit_price: float | None = None
        exit_reason: str | None = None
        exit_i: int | None = None
        end_i = min(len(bars) - 1, entry_i + protocol.MAX_HOLD_HOURS - 1)
        for j in range(entry_i, end_i + 1):
            c = bars[j]
            # Missing hourly bars make path ordering unknowable: fail this signal safely.
            if j > entry_i and c.ts != bars[j - 1].ts + HOUR_MS:
                exit_price = None
                exit_i = None
                break
            if c.low <= stop:
                exit_price, exit_reason, exit_i = stop, "STOP_LOSS", j
                break
            if c.high >= target:
                exit_price, exit_reason, exit_i = target, "TAKE_PROFIT", j
                break
        if exit_i is None:
            if exit_price is None and end_i >= entry_i and all(
                bars[j].ts == bars[j - 1].ts + HOUR_MS for j in range(entry_i + 1, end_i + 1)
            ):
                exit_i = end_i
                exit_price = bars[end_i].close
                exit_reason = "TIME_STOP"
            else:
                continue

        result = compute_trade_costs(
            entry_price=entry,
            exit_price=float(exit_price),
            quantity=quantity,
            side="LONG",
            config=costs,
        )
        net = float(result["net_pnl"])
        closed_ms = bars[exit_i].ts + HOUR_MS
        records.append(
            TradeRecord(
                family_id=str(family["id"]), symbol=symbol,
                signal_ms=signal_ms, entry_ms=entry_bar.ts, closed_ms=closed_ms,
                entry=entry, stop=stop, target=target, exit=float(exit_price),
                exit_reason=str(exit_reason), quantity=quantity,
                net_pnl=net, r_multiple=net / protocol.RISK_AMOUNT_USD,
                features=features,
            )
        )
        busy_until_ms = closed_ms
    return records


def global_deoverlap(records: list[TradeRecord]) -> list[TradeRecord]:
    # Deterministic tie-break fixed before outcomes: entry time then symbol.
    ordered = sorted(records, key=lambda r: (r.entry_ms, r.symbol, r.closed_ms))
    kept: list[TradeRecord] = []
    busy_until = 0
    for record in ordered:
        if record.entry_ms < busy_until:
            continue
        kept.append(record)
        busy_until = record.closed_ms
    return kept


def symbol_robustness(records: list[TradeRecord], holdout_start: str | None) -> dict[str, Any]:
    grouped: dict[str, list[ClosedTrade]] = {}
    if holdout_start:
        for record in records:
            if record.closed_at_utc >= holdout_start:
                grouped.setdefault(record.symbol, []).append(record.closed_trade())
    symbol_results: dict[str, Any] = {}
    evaluable = 0
    profitable = 0
    for symbol, trades in sorted(grouped.items()):
        stats = build_stats(trades)
        is_evaluable = len(trades) >= protocol.MIN_OOS_TRADES_PER_SYMBOL
        is_profitable = bool(
            is_evaluable and isinstance(stats.get("net_pnl"), (int, float)) and stats["net_pnl"] > 0
            and isinstance(stats.get("expectancy_r"), (int, float)) and stats["expectancy_r"] > 0
        )
        evaluable += int(is_evaluable)
        profitable += int(is_profitable)
        symbol_results[symbol] = {"trades": len(trades), "stats": stats, "evaluable": is_evaluable, "profitable": is_profitable}
    ratio = profitable / evaluable if evaluable else 0.0
    passed = evaluable >= protocol.MIN_EVALUABLE_SYMBOLS_OOS and ratio >= protocol.MIN_PROFITABLE_SYMBOL_RATIO
    return {
        "passed": passed,
        "evaluable_symbols": evaluable,
        "profitable_symbols": profitable,
        "profitable_symbol_ratio": round(ratio, 4),
        "required_evaluable_symbols": protocol.MIN_EVALUABLE_SYMBOLS_OOS,
        "required_profitable_symbol_ratio": protocol.MIN_PROFITABLE_SYMBOL_RATIO,
        "symbols": symbol_results,
    }


def evaluate_family(family: dict[str, Any], all_data: dict[str, dict[str, Any]], sample_id: str) -> dict[str, Any]:
    raw: list[TradeRecord] = []
    per_symbol_signal_trades: dict[str, int] = {}
    for symbol, data in sorted(all_data.items()):
        rows = simulate_symbol(data, family)
        per_symbol_signal_trades[symbol] = len(rows)
        raw.extend(rows)
    kept = global_deoverlap(raw)
    trades = [r.closed_trade() for r in kept]
    validation = validate_candidate(
        str(family["id"]), trades,
        sample_id=f"{sample_id}:{family['id']}",
        holdout_fraction=protocol.HOLDOUT_FRACTION,
        window_count=4,
        safety_violations=(),
    )
    generic = promotion_gates(validation)
    cross_symbol = symbol_robustness(kept, validation.get("holdout_start_utc"))
    extra_checks = {
        "accelerator_min_oos_trades": (validation.get("oos_trades") or 0) >= protocol.ACCELERATOR_MIN_OOS_TRADES,
        "accelerator_min_oos_profit_factor": isinstance(validation.get("oos_profit_factor"), (int, float))
            and float(validation["oos_profit_factor"]) >= protocol.ACCELERATOR_MIN_OOS_PROFIT_FACTOR,
        "cross_symbol_robustness": cross_symbol["passed"],
    }
    passed = bool(generic.get("passed")) and all(extra_checks.values())
    return {
        "family": family,
        "raw_signal_trades": len(raw),
        "portfolio_deoverlapped_trades": len(kept),
        "per_symbol_signal_trades": per_symbol_signal_trades,
        "full_stats": build_stats(trades),
        "validation": validation,
        "generic_promotion_gates": generic,
        "cross_symbol_robustness": cross_symbol,
        "extra_accelerator_checks": extra_checks,
        "passed": passed,
        "trade_details": [r.to_dict() for r in kept],
    }


def coverage(data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    adequate = 0
    for symbol, item in sorted(data.items()):
        bars: list[Bar] = item["bars"]
        span_days = (bars[-1].ts - bars[0].ts) / DAY_MS if len(bars) >= 2 else 0.0
        row = {
            "bars": len(bars), "span_days": round(span_days, 2),
            "funding_points": len(item["funding"]), "oi_points": len(item["oi"]), "ratio_points": len(item["ratio"]),
            "from_cache": bool(item.get("from_cache")),
        }
        row["adequate"] = bool(len(bars) >= 24 * 180 and span_days >= 180 and len(item["oi"]) >= 100 and len(item["funding"]) >= protocol.MIN_FUNDING_HISTORY)
        adequate += int(row["adequate"])
        rows[symbol] = row
    return {"adequate_symbols": adequate, "symbols": rows}


def load_universe(lock_path: Path) -> tuple[list[str], str]:
    lock = read_json(lock_path)
    if lock and isinstance(lock.get("symbols"), list) and lock.get("fingerprint"):
        symbols = [str(x).upper() for x in lock["symbols"] if str(x).upper().endswith("USDT")]
        if symbols:
            return symbols, str(lock["fingerprint"])
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    return symbols, "FALLBACK_BTC_ETH_SOL"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="C:/TradingCore_Historical_Accelerator")
    parser.add_argument("--universe-lock", default="C:/TradingCore_Collector_C/data/UNIVERSE_LOCK.json")
    args = parser.parse_args()

    safety = assert_safe_startup()
    state = Path(args.state_dir)
    cache_root = state / "cache"
    state.mkdir(parents=True, exist_ok=True)

    symbols, universe_fp = load_universe(Path(args.universe_lock))
    sample_lock_path = state / "SAMPLE_LOCK.json"
    sample_lock = read_json(sample_lock_path)
    if sample_lock is None:
        end_ms = (int(time.time() * 1000) // HOUR_MS) * HOUR_MS - 1
        start_ms = end_ms - protocol.HISTORY_DAYS * DAY_MS
        sample_lock = {
            "schema": "TRADINGCORE_HISTORICAL_ACCELERATOR_SAMPLE_LOCK_V1",
            "locked_at_utc": datetime.now(timezone.utc).isoformat(),
            "protocol_version": protocol.PROTOCOL_VERSION,
            "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
            "universe_fingerprint": universe_fp,
            "symbols": symbols,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_utc": utc(start_ms),
            "end_utc": utc(end_ms),
            "history_days": protocol.HISTORY_DAYS,
            "real_orders_enabled": False,
            "live_permission": False,
        }
        atomic_json(sample_lock_path, sample_lock)
    else:
        if sample_lock.get("protocol_fingerprint") != protocol.PROTOCOL_FINGERPRINT:
            raise SystemExit("Sample lock protocol fingerprint mismatch; new protocol version required")
        if sample_lock.get("universe_fingerprint") != universe_fp:
            raise SystemExit("Sample lock universe fingerprint mismatch; refusing to change frozen universe")
        symbols = [str(x) for x in sample_lock.get("symbols") or symbols]

    decision_lock_path = state / "HISTORICAL_DECISION_LOCK.json"
    existing_decision = read_json(decision_lock_path)
    if existing_decision and existing_decision.get("protocol_fingerprint") == protocol.PROTOCOL_FINGERPRINT \
            and existing_decision.get("universe_fingerprint") == universe_fp:
        print("=" * 92)
        print("TRADINGCORE HISTORICAL ACCELERATOR — DECISION ALREADY LOCKED")
        print("State:", existing_decision.get("state"))
        print("Candidate:", existing_decision.get("candidate_family"))
        print("No holdout re-opened.")
        print("=" * 92)
        return 0

    print("=" * 92, flush=True)
    print("TRADINGCORE HISTORICAL ACCELERATOR V1", flush=True)
    print("Protocol:", protocol.PROTOCOL_VERSION, protocol.PROTOCOL_FINGERPRINT, flush=True)
    print("Universe:", len(symbols), "symbols", universe_fp, flush=True)
    print("Sample:", sample_lock["start_utc"], "->", sample_lock["end_utc"], flush=True)
    print("Public data only | No API keys | No orders | LIVE disabled", flush=True)
    print("=" * 92, flush=True)

    all_data: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    workers = min(4, max(1, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(load_symbol, symbol, sample_lock, cache_root): symbol for symbol in symbols}
        completed = 0
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                item = future.result()
                all_data[symbol] = item
                completed += 1
                print(
                    f"[{completed}/{len(symbols)}] {symbol}: bars={len(item['bars'])} funding={len(item['funding'])} "
                    f"oi={len(item['oi'])} ratio={len(item['ratio'])} cache={item.get('from_cache')}",
                    flush=True,
                )
            except Exception as exc:
                failures[symbol] = f"{type(exc).__name__}: {exc}"
                print(f"[{symbol}] FAILED SAFE: {failures[symbol]}", flush=True)

    cov = coverage(all_data)
    sample_id = (
        f"{protocol.PROTOCOL_VERSION}:{protocol.PROTOCOL_FINGERPRINT[:16]}:"
        f"{universe_fp[:16]}:{sample_lock['start_ms']}:{sample_lock['end_ms']}"
    )

    family_results: list[dict[str, Any]] = []
    if cov["adequate_symbols"] >= min(protocol.MIN_EVALUABLE_SYMBOLS_OOS, len(symbols)):
        for family in protocol.FAMILIES:
            print(f"Researching {family['id']}...", flush=True)
            family_results.append(evaluate_family(family, all_data, sample_id))

    passed = [r for r in family_results if r.get("passed") is True]
    passed.sort(
        key=lambda r: (
            float((r.get("validation") or {}).get("oos_expectancy_r") or -999),
            float((r.get("validation") or {}).get("oos_profit_factor") or -999),
            int((r.get("validation") or {}).get("oos_trades") or 0),
        ),
        reverse=True,
    )
    candidate = passed[0] if passed else None

    if cov["adequate_symbols"] < min(protocol.MIN_EVALUABLE_SYMBOLS_OOS, len(symbols)):
        final_state = "HISTORICAL_DATA_INSUFFICIENT"
    elif candidate is not None:
        final_state = "HISTORICAL_CANDIDATE_FOUND"
    else:
        final_state = "NO_HISTORICAL_EDGE_FOUND_V1"

    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "state": final_state,
        "protocol": {**protocol.protocol_dict(), "fingerprint": protocol.PROTOCOL_FINGERPRINT},
        "sample_lock": sample_lock,
        "sample_id": sample_id,
        "universe_fingerprint": universe_fp,
        "symbols_requested": symbols,
        "symbols_loaded": sorted(all_data),
        "download_failures": failures,
        "coverage": cov,
        "family_results": family_results,
        "candidate_family": (candidate or {}).get("family", {}).get("id") if candidate else None,
        "candidate_validation": (candidate or {}).get("validation") if candidate else None,
        "candidate_gates": {
            "generic": (candidate or {}).get("generic_promotion_gates") if candidate else None,
            "cross_symbol": (candidate or {}).get("cross_symbol_robustness") if candidate else None,
            "extra": (candidate or {}).get("extra_accelerator_checks") if candidate else None,
        },
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "live_permission": False,
        "collector_a_modified": False,
        "collector_b_modified": False,
        "collector_c_modified": False,
        "note": "This is a historical research decision. Any candidate still requires independent forward PAPER confirmation.",
    }
    atomic_json(state / "LATEST_HISTORICAL_ACCELERATOR.json", report)

    decision = {
        "schema": "TRADINGCORE_HISTORICAL_ACCELERATOR_DECISION_LOCK_V1",
        "locked_at_utc": datetime.now(timezone.utc).isoformat(),
        "state": final_state,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
        "universe_fingerprint": universe_fp,
        "sample_id": sample_id,
        "candidate_family": report["candidate_family"],
        "candidate_validation": report["candidate_validation"],
        "candidate_gates": report["candidate_gates"],
        "holdout_reopen_allowed": False,
        "real_orders_enabled": False,
        "live_permission": False,
    }
    atomic_json(decision_lock_path, decision)

    marker = state / "CANDIDATE_FOR_FORWARD_PAPER.json"
    if candidate is not None:
        atomic_json(
            marker,
            {
                "schema": "TRADINGCORE_HISTORICAL_ACCELERATOR_FORWARD_CANDIDATE_V1",
                "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
                "mode": "PAPER_ONLY",
                "protocol_version": protocol.PROTOCOL_VERSION,
                "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
                "universe_fingerprint": universe_fp,
                "family": candidate["family"],
                "sample_id": sample_id,
                "historical_validation": candidate["validation"],
                "historical_gates": {
                    "generic": candidate["generic_promotion_gates"],
                    "cross_symbol": candidate["cross_symbol_robustness"],
                    "extra": candidate["extra_accelerator_checks"],
                },
                "real_orders_enabled": False,
                "live_permission": False,
            },
        )

    print("", flush=True)
    print("=" * 92, flush=True)
    print("HISTORICAL ACCELERATOR FINAL RESULT", flush=True)
    print("State:", final_state, flush=True)
    print("Adequate symbols:", cov["adequate_symbols"], "/", len(symbols), flush=True)
    for result in family_results:
        v = result.get("validation") or {}
        print(
            f"{result['family']['id']}: PASS={result['passed']} trades={result['portfolio_deoverlapped_trades']} "
            f"OOS={v.get('oos_trades')} PF={v.get('oos_profit_factor')} expR={v.get('oos_expectancy_r')} "
            f"DD_R={v.get('oos_max_drawdown_r')} symbols={result['cross_symbol_robustness'].get('profitable_symbols')}/"
            f"{result['cross_symbol_robustness'].get('evaluable_symbols')}",
            flush=True,
        )
    print("Candidate:", report["candidate_family"], flush=True)
    print("Forward PAPER marker:", str(marker) if candidate is not None else "NOT CREATED", flush=True)
    print("LIVE / real orders: DISABLED", flush=True)
    print("Report:", state / "LATEST_HISTORICAL_ACCELERATOR.json", flush=True)
    print("Decision lock:", decision_lock_path, flush=True)
    print("=" * 92, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
