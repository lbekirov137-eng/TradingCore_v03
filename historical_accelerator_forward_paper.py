#!/usr/bin/env python3
"""Forward PAPER confirmation for Historical Accelerator candidates.

Inert until CANDIDATE_FOR_FORWARD_PAPER.json exists. Uses only public Bybit
market data and simulates a single global long-only spot position. No private
API, account access, order client, or LIVE path exists in this module.
"""
from __future__ import annotations

import argparse
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
import historical_accelerator_protocol as protocol
import historical_accelerator as engine

SCHEMA = "TRADINGCORE_HISTORICAL_ACCELERATOR_FORWARD_PAPER_V1"
HOUR_MS = 3_600_000
MINUTE_MS = 60_000
FORWARD_MIN_CLOSED_TRADES = 30
ENTRY_WINDOW_MINUTES = 10


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
        x = json.loads(path.read_text(encoding="utf-8-sig"))
        return x if isinstance(x, dict) else None
    except Exception:
        return None


def ticker_price(symbol: str) -> float:
    p = engine.http_json("/v5/market/tickers", {"category": "spot", "symbol": symbol})
    rows = ((p.get("result") or {}).get("list") or [])
    if not rows:
        raise RuntimeError(f"No spot ticker for {symbol}")
    value = float(rows[0]["lastPrice"])
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"Invalid ticker for {symbol}")
    return value


def recent_1m_bars(symbol: str, start_ms: int, end_ms: int) -> list[engine.Bar]:
    p = engine.http_json(
        "/v5/market/kline",
        {"category": "spot", "symbol": symbol, "interval": "1", "start": start_ms, "end": end_ms, "limit": 1000},
    )
    out: list[engine.Bar] = []
    for r in ((p.get("result") or {}).get("list") or []):
        try:
            out.append(engine.Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6])))
        except (TypeError, ValueError, IndexError):
            pass
    return sorted(out, key=lambda x: x.ts)


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
        "last_evaluated_hour_ms": None,
        "position": None,
        "closed_trades": [],
        "skipped_hours": [],
        "last_error": None,
        "real_order_sent": False,
    }


def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path)
    if state is None:
        return default_state()
    if state.get("schema") != SCHEMA or state.get("protocol_fingerprint") != protocol.PROTOCOL_FINGERPRINT \
            or state.get("real_order_sent") is not False:
        raise RuntimeError("Forward PAPER state mismatch/unsafe")
    return state


def closed_objects(state: dict[str, Any], family_id: str) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for row in state.get("closed_trades") or []:
        try:
            out.append(ClosedTrade(
                strategy_id=family_id,
                closed_at_utc=str(row["closed_at_utc"]),
                regime=str(row["symbol"]),
                net_pnl=float(row["net_pnl"]),
                r_multiple=float(row["r_multiple"]),
            ))
        except Exception:
            pass
    return out


def current_status(state: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    family = (candidate or {}).get("family") or {}
    family_id = str(family.get("id") or "HISTORICAL_ACCELERATOR")
    trades = closed_objects(state, family_id)
    stats = build_stats(trades)
    historical_validation = (candidate or {}).get("historical_validation") or {}
    validation = {
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
    gates = promotion_gates(validation) if trades else {"passed": False, "failed_gates": ["min_oos_trades"], "checks": []}
    passed = len(trades) >= FORWARD_MIN_CLOSED_TRADES and bool(gates.get("passed"))
    return {
        "schema": SCHEMA,
        "updated_at_utc": now_utc(),
        "state": "WAITING_HISTORICAL_CANDIDATE" if candidate is None else ("FORWARD_PAPER_PASS_OWNER_REVIEW" if passed else "FORWARD_PAPER_RUNNING"),
        "mode": "PAPER_ONLY",
        "family": family_id if candidate else None,
        "authorized": candidate is not None,
        "closed_trades": len(trades),
        "required_closed_trades": FORWARD_MIN_CLOSED_TRADES,
        "stats": stats,
        "promotion_gates_on_forward": gates,
        "forward_pass": passed,
        "position": state.get("position"),
        "last_error": state.get("last_error"),
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "live_permission": False,
        "collector_a_modified": False,
        "collector_b_modified": False,
        "collector_c_modified": False,
    }


def family_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    family = candidate.get("family")
    if not isinstance(family, dict) or not family.get("id"):
        raise RuntimeError("Historical candidate family missing")
    matched = [x for x in protocol.FAMILIES if x["id"] == family["id"]]
    if len(matched) != 1 or matched[0] != family:
        raise RuntimeError("Historical candidate does not match frozen protocol")
    return family


def live_signal(symbol: str, family: dict[str, Any], signal_hour_ms: int) -> tuple[dict[str, Any], float] | None:
    # Signal bar is [signal_hour_ms, signal_hour_ms+1h). We fetch only data that
    # existed by its close. Funding requires a longer rolling window.
    signal_close = signal_hour_ms + HOUR_MS - 1
    bars = engine.fetch_klines(symbol, signal_hour_ms - 20 * 24 * HOUR_MS, signal_close)
    if not bars or bars[-1].ts != signal_hour_ms:
        return None
    i = len(bars) - 1
    if i < max(protocol.ATR_PERIOD, 8):
        return None
    if bars[i - 8].ts != signal_hour_ms - 8 * HOUR_MS or bars[i - 4].ts != signal_hour_ms - 4 * HOUR_MS:
        return None
    funding = engine.fetch_funding(symbol, signal_close - 60 * 24 * HOUR_MS, signal_close)
    oi = engine.fetch_oi(symbol, signal_close - 4 * 24 * HOUR_MS, signal_close)
    ratio = engine.fetch_ratio(symbol, signal_close - 4 * 24 * HOUR_MS, signal_close)
    f_rate, f_z = engine.funding_features(funding, signal_close)
    oi4 = engine.pct_change_series(oi, signal_close, 4)
    oi8 = engine.pct_change_series(oi, signal_close, 8)
    ratio_hit = engine.at_or_before(ratio, signal_close)
    features = {
        "funding": f_rate,
        "funding_z": f_z,
        "return_4h": bars[i].close / bars[i - 4].close - 1.0,
        "return_8h": bars[i].close / bars[i - 8].close - 1.0,
        "oi_change_4h": oi4,
        "oi_change_8h": oi8,
        "buy_ratio": ratio_hit[1] if ratio_hit else None,
        "bullish_signal_candle": bars[i].close > bars[i].open,
    }
    if not engine.family_signal(family, features):
        return None
    atr = engine.atr14(bars, i)
    if atr is None or atr <= 0:
        return None
    return features, atr


def evaluate_new_hour(state: dict[str, Any], candidate: dict[str, Any], universe: dict[str, Any]) -> None:
    if state.get("position"):
        return
    now_ms = int(time.time() * 1000)
    current_hour = (now_ms // HOUR_MS) * HOUR_MS
    signal_hour = current_hour - HOUR_MS
    if state.get("last_evaluated_hour_ms") == signal_hour:
        return
    if now_ms - current_hour > ENTRY_WINDOW_MINUTES * MINUTE_MS:
        state["last_evaluated_hour_ms"] = signal_hour
        state.setdefault("skipped_hours", []).append({"signal_hour_utc": engine.utc(signal_hour), "reason": "MISSED_ENTRY_WINDOW"})
        state["skipped_hours"] = state["skipped_hours"][-200:]
        return

    family = family_from_candidate(candidate)
    signals: list[tuple[str, dict[str, Any], float]] = []
    for symbol in sorted(str(s).upper() for s in universe.get("symbols") or []):
        try:
            result = live_signal(symbol, family, signal_hour)
            if result is not None:
                signals.append((symbol, result[0], result[1]))
        except Exception:
            continue
    state["last_evaluated_hour_ms"] = signal_hour
    if not signals:
        return

    # Same deterministic tie-break as historical portfolio de-overlap.
    symbol, features, atr = sorted(signals, key=lambda x: x[0])[0]
    entry = ticker_price(symbol)
    stop = entry - protocol.ATR_STOP_MULTIPLE * atr
    if stop <= 0 or stop >= entry:
        return
    risk_unit = entry - stop
    quantity = protocol.RISK_AMOUNT_USD / risk_unit
    notional = quantity * entry
    if notional > protocol.REFERENCE_CAPITAL_USD * protocol.MAX_LEVERAGE + 1e-9:
        return
    state["position"] = {
        "mode": "PAPER",
        "side": "LONG",
        "family": family["id"],
        "symbol": symbol,
        "signal_hour_utc": engine.utc(signal_hour),
        "features": features,
        "entry": entry,
        "stop": stop,
        "target": entry + protocol.RISK_REWARD * risk_unit,
        "quantity": quantity,
        "position_notional": notional,
        "opened_ms": now_ms,
        "opened_at_utc": now_utc(),
        "last_checked_ms": now_ms,
        "real_order_sent": False,
    }


def update_position(state: dict[str, Any]) -> None:
    p = state.get("position")
    if not isinstance(p, dict):
        return
    now_ms = int(time.time() * 1000)
    start = max(int(p.get("last_checked_ms") or p["opened_ms"]), now_ms - 2 * HOUR_MS)
    bars = recent_1m_bars(str(p["symbol"]), start, now_ms)
    exit_price: float | None = None
    reason: str | None = None
    for bar in bars:
        if bar.low <= float(p["stop"]):
            exit_price, reason = float(p["stop"]), "STOP_LOSS"
            break
        if bar.high >= float(p["target"]):
            exit_price, reason = float(p["target"]), "TAKE_PROFIT"
            break
    if exit_price is None and now_ms - int(p["opened_ms"]) >= protocol.MAX_HOLD_HOURS * HOUR_MS:
        exit_price, reason = ticker_price(str(p["symbol"])), "TIME_STOP"
    if exit_price is None:
        p["last_checked_ms"] = now_ms
        p["last_price"] = ticker_price(str(p["symbol"]))
        p["last_update_utc"] = now_utc()
        return

    result = compute_trade_costs(
        entry_price=float(p["entry"]), exit_price=float(exit_price), quantity=float(p["quantity"]),
        side="LONG", config=TradingCostConfig(),
    )
    net = float(result["net_pnl"])
    state.setdefault("closed_trades", []).append({
        "family": p["family"], "symbol": p["symbol"],
        "opened_at_utc": p["opened_at_utc"], "closed_at_utc": now_utc(),
        "exit_reason": reason, "entry": p["entry"], "exit": exit_price,
        "quantity": p["quantity"], "net_pnl": net,
        "r_multiple": net / protocol.RISK_AMOUNT_USD,
        "real_order_sent": False,
    })
    state["position"] = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="C:/TradingCore_Historical_Accelerator")
    parser.add_argument("--universe-lock", default="C:/TradingCore_Collector_C/data/UNIVERSE_LOCK.json")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    assert_safe_startup()

    root = Path(args.state_dir)
    root.mkdir(parents=True, exist_ok=True)
    candidate_path = root / "CANDIDATE_FOR_FORWARD_PAPER.json"
    state_path = root / "forward_paper_state.json"
    status_path = root / "forward_paper_status.json"
    owner_path = root / "OWNER_REVIEW_FOR_MICRO_LIVE.json"
    universe_path = Path(args.universe_lock)
    state = load_state(state_path)

    while True:
        try:
            candidate = read_json(candidate_path)
            universe = read_json(universe_path)
            if candidate:
                if candidate.get("protocol_fingerprint") != protocol.PROTOCOL_FINGERPRINT or candidate.get("mode") != "PAPER_ONLY":
                    raise RuntimeError("Historical candidate authorization mismatch")
                if not universe or candidate.get("universe_fingerprint") != universe.get("fingerprint"):
                    raise RuntimeError("Forward universe mismatch")
                update_position(state)
                evaluate_new_hour(state, candidate, universe)
            state["last_error"] = None
            state["real_order_sent"] = False
            atomic_json(state_path, state)
            status = current_status(state, candidate)
            atomic_json(status_path, status)
            if status.get("forward_pass") is True and not owner_path.exists():
                atomic_json(owner_path, {
                    "schema": "TRADINGCORE_HISTORICAL_ACCELERATOR_OWNER_REVIEW_V1",
                    "created_at_utc": now_utc(),
                    "state": "OWNER_REVIEW_REQUIRED",
                    "family": status.get("family"),
                    "forward_closed_trades": status.get("closed_trades"),
                    "forward_stats": status.get("stats"),
                    "live_enabled": False,
                    "real_orders_enabled": False,
                    "note": "Historical + independent forward PAPER gates passed. LIVE remains disabled and requires a separate execution design plus explicit owner approval.",
                })
        except Exception as exc:
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            state["real_order_sent"] = False
            atomic_json(state_path, state)
            atomic_json(status_path, {
                "schema": SCHEMA, "updated_at_utc": now_utc(), "state": "FAILED_SAFELY_RETRYING",
                "last_error": state["last_error"], "real_orders_enabled": False,
                "real_order_sent": False, "live_permission": False,
            })
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
