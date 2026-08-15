#!/usr/bin/env python3
"""Run one Fast PAPER Lab cloud cycle.

Each cycle reconstructs all forward PAPER lanes from public closed Binance spot
candles after the frozen start timestamp. This makes the runner restart-safe and
independent of any laptop process. Lane decisions are locked on the FIRST 30
closed forward trades only; later trades cannot rescue a failed first decision.

PAPER / RESEARCH ONLY. No authenticated API, balances, or order code.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_engine.strategies.contracts import Candle, StrategyConfig
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from config.startup_safety import assert_safe_startup
from fast_paper_protocol import (
    FORWARD_FREEZE_UTC,
    HYPOTHESES,
    MAX_DRAWDOWN_R,
    MIN_EXPECTANCY_R,
    MIN_FORWARD_CLOSED_TRADES,
    MIN_PROFIT_FACTOR,
    MIN_SEGMENT_ROBUSTNESS,
    MIN_TRADING_DAYS,
    PROTOCOL_FINGERPRINT,
    PROTOCOL_VERSION,
    RISK_AMOUNT_USD,
    SYMBOLS,
    TIMEFRAMES,
    WARMUP_BARS,
)
from fast_paper_strategies import STRATEGY_CLASSES

SCHEMA = "TRADINGCORE_FAST_PAPER_CLOUD_V1"
BINANCE_BASE = "https://data-api.binance.vision"
KLINES_PATH = "/api/v3/klines"
DAY_MS = 86_400_000
FREEZE_MS = int(datetime.fromisoformat(FORWARD_FREEZE_UTC).timestamp() * 1000)


def utc_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def request_klines(params: dict[str, Any], attempts: int = 6) -> list[list[Any]]:
    query = urlencode(params)
    url = f"{BINANCE_BASE}{KLINES_PATH}?{query}"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "TradingCore-FastPaper/1.0"},
                method="GET",
            )
            with urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError(f"Binance payload is not a list: {type(payload).__name__}")
            return payload
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
    raise RuntimeError(f"Binance public kline request failed: {last}")


def fetch_closed_candles(symbol: str, timeframe: str) -> list[Candle]:
    meta = TIMEFRAMES[timeframe]
    interval_ms = int(meta["interval_ms"])
    now_ms = int(time.time() * 1000)
    # Seven warm-up days safely exceed 60 bars for both 15m and 30m lanes.
    start_ms = FREEZE_MS - 7 * DAY_MS
    cursor = start_ms
    by_time: dict[int, Candle] = {}

    while cursor < now_ms:
        rows = request_klines({
            "symbol": symbol,
            "interval": timeframe,
            "startTime": cursor,
            "endTime": now_ms - 1,
            "limit": 1000,
        })
        if not rows:
            break
        last_open = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                continue
            try:
                open_ms = int(row[0])
                close_ms = int(row[6])
                if close_ms >= now_ms:
                    continue
                by_time[open_ms] = Candle(
                    open_time_ms=open_ms,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
                last_open = open_ms
            except (TypeError, ValueError):
                continue
        if last_open is None or len(rows) < 1000:
            break
        next_cursor = int(last_open) + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.03)

    return sorted(by_time.values(), key=lambda c: int(c.open_time_ms))


def segment_robustness(trades: list[ClosedTrade], segments: int = 4) -> float | None:
    if len(trades) < 8:
        return None
    size = max(1, len(trades) // segments)
    groups: list[list[ClosedTrade]] = []
    for index in range(segments):
        start = index * size
        end = (index + 1) * size if index < segments - 1 else len(trades)
        block = trades[start:end]
        if block:
            groups.append(block)
    if not groups:
        return None
    profitable = 0
    for block in groups:
        stats = build_stats(block)
        net = stats.get("net_pnl")
        if isinstance(net, (int, float)) and float(net) > 0:
            profitable += 1
    return round(profitable / len(groups), 4)


def simulate_lane(
    symbol: str,
    timeframe: str,
    hypothesis: str,
    candles: list[Candle],
) -> dict[str, Any]:
    meta = TIMEFRAMES[timeframe]
    interval_ms = int(meta["interval_ms"])
    max_bars = int(meta["max_bars_in_trade"])
    cls = STRATEGY_CLASSES[hypothesis]
    strategy = cls(StrategyConfig(warmup_bars=WARMUP_BARS))
    lane_id = f"{symbol}:{timeframe}:{hypothesis}"
    strategy.strategy_key = f"FAST_{hypothesis}_{timeframe.upper()}"

    cost_config = TradingCostConfig()
    trades: list[ClosedTrade] = []
    details: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    signals = 0

    for index, candle in enumerate(candles):
        close_ms = int(candle.open_time_ms) + interval_ms

        if open_trade is not None:
            exit_price = None
            exit_reason = None
            if float(candle.low) <= float(open_trade["stop"]):
                exit_price = float(open_trade["stop"])
                exit_reason = "STOP_LOSS"
            elif float(candle.high) >= float(open_trade["target"]):
                exit_price = float(open_trade["target"])
                exit_reason = "TAKE_PROFIT"
            elif index - int(open_trade["entry_index"]) >= max_bars:
                exit_price = float(candle.close)
                exit_reason = "TIME_STOP"

            if exit_price is not None:
                costs = compute_trade_costs(
                    entry_price=float(open_trade["entry"]),
                    exit_price=exit_price,
                    quantity=float(open_trade["quantity"]),
                    side="LONG",
                    config=cost_config,
                )
                risk = float(open_trade["risk_amount"])
                r_multiple = float(costs["net_pnl"]) / risk if risk > 0 else None
                closed_at = utc_ms(close_ms)
                trades.append(ClosedTrade(lane_id, closed_at, symbol, float(costs["net_pnl"]), r_multiple))
                details.append({
                    "entry_utc": open_trade["entry_utc"],
                    "closed_utc": closed_at,
                    "entry": open_trade["entry"],
                    "exit": exit_price,
                    "exit_reason": exit_reason,
                    "net_pnl": costs["net_pnl"],
                    "r_multiple": r_multiple,
                    "real_order_sent": False,
                })
                open_trade = None

        if open_trade is not None:
            continue
        if close_ms <= FREEZE_MS:
            continue

        decision = strategy.evaluate_closed_candle(candles, index)
        if not decision.is_trade:
            continue
        signals += 1
        entry = float(decision.entry)
        stop = float(decision.stop)
        target = float(decision.take_profit_2)
        if not (0 < stop < entry < target):
            continue
        risk_per_unit = entry - stop
        quantity = RISK_AMOUNT_USD / risk_per_unit
        if not math.isfinite(quantity) or quantity <= 0:
            continue
        notional = quantity * entry
        if notional > 1000.000001:
            continue
        open_trade = {
            "entry": entry,
            "stop": stop,
            "target": target,
            "quantity": quantity,
            "risk_amount": RISK_AMOUNT_USD,
            "entry_index": index,
            "entry_utc": utc_ms(close_ms),
        }

    stats = build_stats(trades)
    robust = segment_robustness(trades)
    return {
        "lane_id": lane_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "hypothesis": hypothesis,
        "signals": signals,
        "closed_trades": len(trades),
        "stats": stats,
        "segment_robustness": robust,
        "open_position": open_trade,
        "trades": details,
    }


def first30_decision(lane: dict[str, Any]) -> dict[str, Any]:
    first = lane.get("trades") or []
    first = first[:MIN_FORWARD_CLOSED_TRADES]
    closed: list[ClosedTrade] = []
    for item in first:
        closed.append(ClosedTrade(
            lane.get("lane_id"),
            str(item.get("closed_utc")),
            str(lane.get("symbol")),
            float(item.get("net_pnl")),
            float(item.get("r_multiple")),
        ))
    stats = build_stats(closed)
    robust = segment_robustness(closed)
    checks = {
        "exact_first_30": len(closed) == MIN_FORWARD_CLOSED_TRADES,
        "profit_factor": isinstance(stats.get("profit_factor"), (int, float)) and float(stats["profit_factor"]) >= MIN_PROFIT_FACTOR,
        "expectancy": isinstance(stats.get("expectancy_r"), (int, float)) and float(stats["expectancy_r"]) > MIN_EXPECTANCY_R,
        "net_positive": isinstance(stats.get("net_pnl"), (int, float)) and float(stats["net_pnl"]) > 0,
        "drawdown": isinstance(stats.get("max_drawdown_r"), (int, float)) and float(stats["max_drawdown_r"]) <= MAX_DRAWDOWN_R,
        "segment_robustness": isinstance(robust, (int, float)) and float(robust) >= MIN_SEGMENT_ROBUSTNESS,
        "trading_days": int(stats.get("trading_days") or 0) >= MIN_TRADING_DAYS,
    }
    passed = all(checks.values())
    return {
        "state": "PAPER_PROMISING_REQUIRES_INDEPENDENT_CONFIRMATION" if passed else "FIRST30_REJECTED_FROZEN",
        "locked_at_utc": now_utc(),
        "forward_rule": "FIRST_30_CLOSED_TRADES_ONLY",
        "stats": stats,
        "segment_robustness": robust,
        "checks": checks,
        "live_permission": False,
        "real_orders_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="fast_paper_runtime")
    args = parser.parse_args()

    safety = assert_safe_startup()
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = state_dir / "FAST_PAPER_DECISIONS.json"
    existing = read_json(decisions_path) or {
        "schema": "TRADINGCORE_FAST_PAPER_DECISIONS_V1",
        "protocol_fingerprint": PROTOCOL_FINGERPRINT,
        "lanes": {},
        "real_orders_enabled": False,
        "live_permission": False,
    }
    if existing.get("protocol_fingerprint") != PROTOCOL_FINGERPRINT:
        raise RuntimeError("Fast PAPER decision protocol fingerprint mismatch")
    decisions = existing.get("lanes") if isinstance(existing.get("lanes"), dict) else {}

    market: dict[tuple[str, str], list[Candle]] = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            rows = fetch_closed_candles(symbol, timeframe)
            market[(symbol, timeframe)] = rows
            print(f"FAST_DATA {symbol} {timeframe} closed_bars={len(rows)}", flush=True)

    lanes: list[dict[str, Any]] = []
    new_decisions: list[str] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            rows = market[(symbol, timeframe)]
            for hypothesis in HYPOTHESES:
                lane = simulate_lane(symbol, timeframe, hypothesis, rows)
                lane_id = lane["lane_id"]
                if lane_id not in decisions and int(lane["closed_trades"]) >= MIN_FORWARD_CLOSED_TRADES:
                    decisions[lane_id] = first30_decision(lane)
                    new_decisions.append(lane_id)
                lane["decision"] = decisions.get(lane_id)
                lanes.append(lane)
                stats = lane["stats"]
                print(
                    f"FAST_LANE {lane_id} closed={lane['closed_trades']} PF={stats.get('profit_factor')} "
                    f"expR={stats.get('expectancy_r')} DD={stats.get('max_drawdown_r')} decision={(lane.get('decision') or {}).get('state')}",
                    flush=True,
                )

    existing["lanes"] = decisions
    existing["updated_at_utc"] = now_utc()
    atomic_json(decisions_path, existing)

    promising = [lane_id for lane_id, decision in decisions.items() if isinstance(decision, dict) and decision.get("state") == "PAPER_PROMISING_REQUIRES_INDEPENDENT_CONFIRMATION"]
    status = {
        "schema": SCHEMA,
        "updated_at_utc": now_utc(),
        "mode": "EXPERIMENTAL_FAST_PAPER_ONLY",
        "protocol_version": PROTOCOL_VERSION,
        "protocol_fingerprint": PROTOCOL_FINGERPRINT,
        "forward_freeze_utc": FORWARD_FREEZE_UTC,
        "lane_count": len(lanes),
        "total_closed_trade_observations_across_independent_lanes": sum(int(lane["closed_trades"]) for lane in lanes),
        "lanes_with_30_or_more": sum(1 for lane in lanes if int(lane["closed_trades"]) >= MIN_FORWARD_CLOSED_TRADES),
        "locked_lane_decisions": len(decisions),
        "promising_lanes": promising,
        "new_decisions_this_run": new_decisions,
        "lanes": lanes,
        "safety": safety,
        "stable_btc_1h_champion_modified": False,
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "live_permission": False,
        "note": "Each lane is independent. Aggregate trade counts/PnL are not evidence of profitability. A PAPER-promising lane still requires an independent confirmatory test.",
    }
    atomic_json(state_dir / "FAST_PAPER_STATUS.json", status)

    print("=" * 92)
    print("TRADINGCORE FAST PAPER CLOUD RESULT")
    print("Lanes:", len(lanes))
    print("Total independent lane trade observations:", status["total_closed_trade_observations_across_independent_lanes"])
    print("Lanes >=30:", status["lanes_with_30_or_more"])
    print("Promising lanes:", promising or "NONE")
    print("New decisions:", new_decisions or "NONE")
    print("Stable BTC 1H champion: UNCHANGED")
    print("LIVE / real orders: DISABLED")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
