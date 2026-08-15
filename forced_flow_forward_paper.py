#!/usr/bin/env python3
"""Forward PAPER worker for the preregistered forced-flow strategy.

May run continuously before research completes. It remains inert until
C:/TradingCore_Autonomous/FORWARD_PAPER_AUTHORIZED_BY_RESEARCH.json exists.
No private API, no account access and no order code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from config.startup_safety import assert_safe_startup

import forced_flow_protocol as protocol
from forced_flow_research_engine import (
    atr14,
    build_clusters,
    ceil_minute,
    fetch_1m_klines,
    read_events,
)

SCHEMA = "TRADINGCORE_FORCED_FLOW_FORWARD_PAPER_V1"
BYBIT_REST = "https://api.bybit.com"
MINUTE_MS = 60_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


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


def _http_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{BYBIT_REST}{path}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "TradingCore-ForcedFlow-ForwardPaper/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit public request failed: {payload}")
    return payload


def latest_price(symbol: str) -> float:
    payload = _http_json("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    rows = ((payload.get("result") or {}).get("list") or [])
    if not rows or not isinstance(rows[0], dict):
        raise RuntimeError(f"No public ticker for {symbol}")
    price = float(rows[0]["lastPrice"])
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError(f"Invalid public ticker for {symbol}: {price}")
    return price


def cluster_id(cluster: Any) -> str:
    raw = "|".join(
        [
            cluster.symbol,
            cluster.side,
            str(cluster.start_ms),
            str(cluster.end_ms),
            *cluster.keys,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
        "processed_cluster_ids": [],
        "pending": {},
        "positions": {},
        "closed_trades": [],
        "skipped": {},
        "last_error": None,
        "real_order_sent": False,
    }


def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path)
    if state is None:
        return default_state()
    if state.get("schema") != SCHEMA:
        raise RuntimeError("Forward PAPER state schema mismatch")
    if state.get("protocol_fingerprint") != protocol.PROTOCOL_FINGERPRINT:
        raise RuntimeError("Forward PAPER state belongs to a different protocol")
    if state.get("real_order_sent") is not False:
        raise RuntimeError("Unsafe forward PAPER state")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at_utc"] = utc_now()
    state["real_order_sent"] = False
    atomic_json(path, state)


def closed_trade_objects(state: dict[str, Any]) -> list[ClosedTrade]:
    result: list[ClosedTrade] = []
    for row in state.get("closed_trades") or []:
        if not isinstance(row, dict):
            continue
        try:
            result.append(
                ClosedTrade(
                    strategy_id=protocol.STRATEGY_ID,
                    closed_at_utc=str(row["closed_at_utc"]),
                    regime=str(row["symbol"]),
                    net_pnl=float(row["net_pnl"]),
                    r_multiple=float(row["r_multiple"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def build_status(state: dict[str, Any], auth: dict[str, Any] | None, decision_lock: dict[str, Any] | None) -> dict[str, Any]:
    trades = closed_trade_objects(state)
    stats = build_stats(trades)
    historical_validation = (decision_lock or {}).get("validation") or {}
    forward_validation = {
        "oos_trades": stats.get("closed_trades"),
        "oos_net_pnl": stats.get("net_pnl"),
        "oos_profit_factor": stats.get("profit_factor"),
        "oos_expectancy_r": stats.get("expectancy_r"),
        "oos_max_drawdown_r": stats.get("max_drawdown_r"),
        "safety_violations": [],
        "robustness_ratio": historical_validation.get("robustness_ratio"),
        "walk_forward_passed": historical_validation.get("walk_forward_passed") is True,
        "look_ahead_leakage": False,
    }
    gates = promotion_gates(forward_validation) if trades else {"passed": False, "failed_gates": ["min_oos_trades"], "checks": []}
    enough_forward = len(trades) >= protocol.FORWARD_PAPER_MIN_CLOSED_TRADES
    forward_pass = bool(enough_forward and gates.get("passed"))

    return {
        "schema": SCHEMA,
        "updated_at_utc": utc_now(),
        "state": (
            "WAITING_HISTORICAL_RESEARCH_PASS"
            if auth is None
            else ("FORWARD_PAPER_PASS_OWNER_REVIEW" if forward_pass else "FORWARD_PAPER_RUNNING")
        ),
        "mode": "PAPER_ONLY",
        "protocol_version": protocol.PROTOCOL_VERSION,
        "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
        "authorized": auth is not None,
        "forward_freeze_utc": auth.get("authorized_at_utc") if auth else None,
        "closed_trades": len(trades),
        "required_closed_trades": protocol.FORWARD_PAPER_MIN_CLOSED_TRADES,
        "stats": stats,
        "promotion_gates_on_forward": gates,
        "forward_pass": forward_pass,
        "pending": state.get("pending"),
        "positions": state.get("positions"),
        "last_error": state.get("last_error"),
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "live_permission": False,
        "collector_a_modified": False,
    }


def process_new_clusters(state: dict[str, Any], data_dir: Path, freeze_ms: int) -> None:
    events = [event for event in read_events(data_dir) if event.event_ts_ms > freeze_ms]
    clusters = build_clusters(events)
    processed = set(str(value) for value in state.get("processed_cluster_ids") or [])
    pending = state.setdefault("pending", {})
    positions = state.setdefault("positions", {})
    now_ms = int(time.time() * 1000)

    for cluster in clusters:
        if cluster.side != protocol.LIQUIDATED_SIDE:
            continue
        if cluster.aggregate_notional_usdt < protocol.PRIMARY_THRESHOLD_USDT:
            continue
        if now_ms - cluster.end_ms < protocol.CLUSTER_WINDOW_SECONDS * 1000:
            continue
        cid = cluster_id(cluster)
        if cid in processed:
            continue
        processed.add(cid)
        if cluster.symbol in positions or cluster.symbol in pending:
            state.setdefault("skipped", {})[cid] = "SYMBOL_BUSY"
            continue
        pending[cluster.symbol] = {
            "cluster_id": cid,
            "cluster_start_ms": cluster.start_ms,
            "cluster_end_ms": cluster.end_ms,
            "cluster_notional_usdt": cluster.aggregate_notional_usdt,
            "cluster_events": cluster.event_count,
            "created_at_utc": utc_now(),
        }

    state["processed_cluster_ids"] = list(processed)[-10000:]


def process_pending(state: dict[str, Any]) -> None:
    pending = state.setdefault("pending", {})
    positions = state.setdefault("positions", {})
    now_ms = int(time.time() * 1000)

    for symbol in list(pending):
        item = pending[symbol]
        cluster_end = int(item["cluster_end_ms"])
        stabilisation_start = ceil_minute(cluster_end)
        entry_start = stabilisation_start + MINUTE_MS
        # Wait for the stabilisation candle to fully close.
        if now_ms < entry_start:
            continue
        # Realistic forward execution: if the process was not present around the
        # scheduled entry, do not backfill an imaginary fill at candle open.
        if now_ms > entry_start + 15_000:
            state.setdefault("skipped", {})[item["cluster_id"]] = "MISSED_15S_ENTRY_WINDOW"
            pending.pop(symbol, None)
            continue

        rows = fetch_1m_klines(symbol, stabilisation_start - 40 * MINUTE_MS, stabilisation_start + MINUTE_MS)
        by_time = {row.start_ms: index for index, row in enumerate(rows)}
        idx = by_time.get(stabilisation_start)
        if idx is None or idx < protocol.ATR_PERIOD + 1:
            continue
        stabilisation = rows[idx]
        if not stabilisation.close > stabilisation.open:
            state.setdefault("skipped", {})[item["cluster_id"]] = "STABILISATION_NOT_BULLISH"
            pending.pop(symbol, None)
            continue
        atr_value = atr14(rows[:idx])
        if atr_value is None or atr_value <= 0:
            continue

        entry = latest_price(symbol)
        stop = entry - protocol.ATR_STOP_MULTIPLE * atr_value
        if stop <= 0 or stop >= entry:
            pending.pop(symbol, None)
            continue
        risk_per_unit = entry - stop
        quantity = protocol.RISK_AMOUNT_USD / risk_per_unit
        notional = quantity * entry
        if notional > protocol.REFERENCE_CAPITAL_USD * protocol.MAX_LEVERAGE + 1e-9:
            state.setdefault("skipped", {})[item["cluster_id"]] = "STOP_TOO_TIGHT_FOR_1X"
            pending.pop(symbol, None)
            continue
        target = entry + protocol.RISK_REWARD * risk_per_unit
        positions[symbol] = {
            "mode": "PAPER",
            "side": "LONG",
            "cluster_id": item["cluster_id"],
            "entry": entry,
            "stop": stop,
            "target": target,
            "quantity": quantity,
            "risk_amount": protocol.RISK_AMOUNT_USD,
            "position_notional": notional,
            "opened_at_utc": utc_now(),
            "opened_ms": now_ms,
            "real_order_sent": False,
        }
        pending.pop(symbol, None)


def process_positions(state: dict[str, Any]) -> None:
    positions = state.setdefault("positions", {})
    closed = state.setdefault("closed_trades", [])
    now_ms = int(time.time() * 1000)
    costs = TradingCostConfig()

    for symbol in list(positions):
        position = positions[symbol]
        price = latest_price(symbol)
        exit_reason = None
        if price <= float(position["stop"]):
            exit_reason = "STOP_LOSS"
        elif price >= float(position["target"]):
            exit_reason = "TAKE_PROFIT"
        elif now_ms - int(position["opened_ms"]) >= protocol.MAX_HOLD_MINUTES * MINUTE_MS:
            exit_reason = "TIME_STOP"
        if exit_reason is None:
            position["last_price"] = price
            position["last_update_utc"] = utc_now()
            continue

        result = compute_trade_costs(
            entry_price=float(position["entry"]),
            exit_price=price,
            quantity=float(position["quantity"]),
            side="LONG",
            config=costs,
        )
        net_pnl = float(result["net_pnl"])
        closed.append(
            {
                "symbol": symbol,
                "cluster_id": position["cluster_id"],
                "opened_at_utc": position["opened_at_utc"],
                "closed_at_utc": utc_now(),
                "exit_reason": exit_reason,
                "entry": position["entry"],
                "exit": price,
                "quantity": position["quantity"],
                "net_pnl": net_pnl,
                "r_multiple": net_pnl / protocol.RISK_AMOUNT_USD,
                "real_order_sent": False,
            }
        )
        positions.pop(symbol, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="C:/TradingCore_Collector_B/data")
    parser.add_argument("--state-dir", default="C:/TradingCore_Autonomous")
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()

    assert_safe_startup()
    data_dir = Path(args.data_dir)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "forward_paper_state.json"
    status_path = state_dir / "forward_paper_status.json"
    auth_path = state_dir / "FORWARD_PAPER_AUTHORIZED_BY_RESEARCH.json"
    lock_path = state_dir / "historical_decision_lock.json"
    owner_review = state_dir / "OWNER_REVIEW_FOR_MICRO_LIVE.json"
    state = load_state(state_path)

    while True:
        try:
            auth = read_json(auth_path)
            lock = read_json(lock_path)
            if auth is not None:
                if auth.get("protocol_fingerprint") != protocol.PROTOCOL_FINGERPRINT:
                    raise RuntimeError("Forward authorization protocol mismatch")
                freeze_ms = parse_ms(str(auth["authorized_at_utc"]))
                process_new_clusters(state, data_dir, freeze_ms)
                process_pending(state)
                process_positions(state)
            state["last_error"] = None
            save_state(state_path, state)
            status = build_status(state, auth, lock)
            atomic_json(status_path, status)
            if status.get("forward_pass") is True and not owner_review.exists():
                atomic_json(
                    owner_review,
                    {
                        "schema": "TRADINGCORE_OWNER_MICRO_LIVE_REVIEW_V1",
                        "created_at_utc": utc_now(),
                        "state": "OWNER_REVIEW_REQUIRED",
                        "protocol_version": protocol.PROTOCOL_VERSION,
                        "forward_closed_trades": status.get("closed_trades"),
                        "forward_stats": status.get("stats"),
                        "live_enabled": False,
                        "real_orders_enabled": False,
                        "note": "Historical + forward PAPER gates passed. LIVE remains disabled pending explicit owner review and separate execution architecture.",
                    },
                )
        except Exception as error:
            state["last_error"] = f"{type(error).__name__}: {error}"
            save_state(state_path, state)
            atomic_json(
                status_path,
                {
                    "schema": SCHEMA,
                    "state": "FAILED_SAFELY_RETRYING",
                    "last_error": state["last_error"],
                    "real_orders_enabled": False,
                    "real_order_sent": False,
                    "live_permission": False,
                    "updated_at_utc": utc_now(),
                },
            )
        time.sleep(max(2, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
