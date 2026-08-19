#!/usr/bin/env python3
"""TradingCore BTC Delta-Neutral Funding Carry — OKX Forward Shadow V1.

Frozen strategy selected by Funding Carry V3:
- long BTC-USDT spot and short BTC-USDT-SWAP with equal USD notionals;
- enter only when the average of the last three realized funding events is
  at least 1.5 basis points per event;
- require entry basis between -25 bps and +2.00%;
- exit when rolling funding decays to <= 0, basis widens by 2.50% from entry,
  or the position has been held for 30 days.

Execution is modeled from public OKX order books: next cycle ask for the spot
long, bid for the perp short, then bid/ask on exit, plus TradingCore's
conservative fees and slippage. Public data and PAPER state only. No private API,
credentials, balances, transfers, or order placement.
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
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_FUNDING_CARRY_FORWARD_OKX_V1"
OKX = "https://www.okx.com"
SPOT_INST = "BTC-USDT"
SWAP_INST = "BTC-USDT-SWAP"
CAPITAL_USD = 1000.0
LEG_NOTIONAL_USD = 500.0
LOOKBACK_EVENTS = 3
ENTRY_FUNDING_THRESHOLD = 0.00015
MIN_ENTRY_BASIS = -0.0025
MAX_ENTRY_BASIS = 0.0200
BASIS_STOP_WIDENING = 0.0250
MAX_HOLD_SECONDS = 30 * 86_400
MIN_FORWARD_TRADES_FOR_REVIEW = 5


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def request_json(url: str, attempts: int = 6) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "TradingCore-FundingForward/1.0",
                    "Accept": "application/json",
                },
            )
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError(last)


def order_book(instrument: str) -> dict[str, float]:
    params = urlencode({"instId": instrument, "sz": 5})
    payload = request_json(f"{OKX}/api/v5/market/books?{params}")
    if str(payload.get("code")) != "0" or not payload.get("data"):
        raise RuntimeError(f"OKX order-book error for {instrument}: {payload}")
    row = payload["data"][0]
    bids = row.get("bids") or []
    asks = row.get("asks") or []
    if not bids or not asks:
        raise RuntimeError(f"empty OKX order book for {instrument}")
    bid = float(bids[0][0])
    ask = float(asks[0][0])
    if not (bid > 0 and ask >= bid):
        raise RuntimeError(f"invalid book for {instrument}: bid={bid}, ask={ask}")
    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0,
        "spread_bps": (ask / bid - 1.0) * 10_000.0,
    }


def realized_funding_history(limit: int = 100) -> list[dict[str, Any]]:
    params = urlencode({"instId": SWAP_INST, "limit": limit})
    payload = request_json(f"{OKX}/api/v5/public/funding-rate-history?{params}")
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX funding-history error: {payload}")
    events: dict[int, dict[str, Any]] = {}
    for row in payload.get("data") or []:
        try:
            timestamp = int(row["fundingTime"])
            rate = float(row.get("realizedRate") or row.get("fundingRate"))
            if finite(rate):
                events[timestamp] = {
                    "timestamp_ms": timestamp,
                    "utc": datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).isoformat(),
                    "rate": rate,
                }
        except Exception:
            continue
    return [events[key] for key in sorted(events)]


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temp.replace(path)


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "freeze_utc": iso_now(),
        "last_cycle_utc": None,
        "cycle_count": 0,
        "signal_count": 0,
        "position": None,
        "trades": [],
        "last_processed_funding_timestamp_ms": None,
        "errors": [],
        "real_orders_enabled": False,
        "live_permission": False,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        state.setdefault("schema", SCHEMA)
        state.setdefault("freeze_utc", iso_now())
        state.setdefault("cycle_count", 0)
        state.setdefault("signal_count", 0)
        state.setdefault("position", None)
        state.setdefault("trades", [])
        state.setdefault("errors", [])
        state["real_orders_enabled"] = False
        state["live_permission"] = False
        return state
    except Exception as exc:
        state = default_state()
        state["errors"].append({"utc": iso_now(), "error": f"STATE_RESET: {type(exc).__name__}: {exc}"})
        return state


def trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["return_fraction"]) for row in trades if finite(row.get("return_fraction"))]
    n = len(returns)
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = peak = max_drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(100.0 * len(wins) / n, 2) if n else None,
        "net_return_percent": round(100.0 * sum(returns), 5) if n else None,
        "average_return_bps": round(10_000.0 * sum(returns) / n, 4) if n else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else (99.0 if wins else None),
        "max_drawdown_percent": round(100.0 * max_drawdown, 5) if n else None,
    }


def close_position(
    state: dict[str, Any],
    spot_book: dict[str, float],
    swap_book: dict[str, float],
    reason: str,
) -> None:
    position = state.get("position")
    if not isinstance(position, dict):
        return
    config = TradingCostConfig()
    spot_result = compute_trade_costs(
        entry_price=float(position["spot_entry_ask"]),
        exit_price=float(spot_book["bid"]),
        quantity=float(position["spot_quantity"]),
        side="LONG",
        config=config,
    )
    swap_result = compute_trade_costs(
        entry_price=float(position["swap_entry_bid"]),
        exit_price=float(swap_book["ask"]),
        quantity=float(position["swap_quantity"]),
        side="SHORT",
        config=config,
    )
    funding_pnl = float(position.get("funding_pnl_usd") or 0.0)
    total_pnl = float(spot_result["net_pnl"]) + float(swap_result["net_pnl"]) + funding_pnl
    result_fraction = total_pnl / CAPITAL_USD
    exit_basis = swap_book["mid"] / spot_book["mid"] - 1.0
    state["trades"].append(
        {
            "entry_utc": position["entry_utc"],
            "exit_utc": iso_now(),
            "reason": reason,
            "spot_entry_ask": position["spot_entry_ask"],
            "spot_exit_bid": spot_book["bid"],
            "swap_entry_bid": position["swap_entry_bid"],
            "swap_exit_ask": swap_book["ask"],
            "entry_basis": position["entry_basis"],
            "exit_basis": exit_basis,
            "funding_events_collected": position.get("funding_events_collected", 0),
            "funding_pnl_usd": round(funding_pnl, 8),
            "spot_net_pnl_usd": spot_result["net_pnl"],
            "swap_net_pnl_usd": swap_result["net_pnl"],
            "total_net_pnl_usd": round(total_pnl, 8),
            "return_fraction": result_fraction,
            "return_percent": round(100.0 * result_fraction, 5),
        }
    )
    state["position"] = None


def run_cycle(root: Path) -> dict[str, Any]:
    state_path = root / "FUNDING_CARRY_FORWARD_STATE.json"
    status_path = root / "FUNDING_CARRY_FORWARD_STATUS.json"
    state = load_state(state_path)
    now = utc_now()
    state["cycle_count"] = int(state.get("cycle_count") or 0) + 1
    state["last_cycle_utc"] = now.isoformat()
    safety = assert_safe_startup()

    try:
        spot_book = order_book(SPOT_INST)
        swap_book = order_book(SWAP_INST)
        funding = realized_funding_history(100)
        if len(funding) < LOOKBACK_EVENTS:
            raise RuntimeError(f"only {len(funding)} realized funding events available")

        latest_three = funding[-LOOKBACK_EVENTS:]
        avg_funding = sum(float(row["rate"]) for row in latest_three) / LOOKBACK_EVENTS
        basis = swap_book["mid"] / spot_book["mid"] - 1.0
        signal_active = (
            avg_funding >= ENTRY_FUNDING_THRESHOLD
            and MIN_ENTRY_BASIS <= basis <= MAX_ENTRY_BASIS
        )

        position = state.get("position")
        if isinstance(position, dict):
            last_processed = int(position.get("last_processed_funding_timestamp_ms") or 0)
            for event in funding:
                event_ts = int(event["timestamp_ms"])
                if event_ts <= last_processed:
                    continue
                if event_ts > int(position["entry_timestamp_ms"]):
                    payment = LEG_NOTIONAL_USD * float(event["rate"])
                    position["funding_pnl_usd"] = float(position.get("funding_pnl_usd") or 0.0) + payment
                    position["funding_events_collected"] = int(position.get("funding_events_collected") or 0) + 1
                position["last_processed_funding_timestamp_ms"] = event_ts
            state["position"] = position

            held_seconds = max(0.0, now.timestamp() - parse_time(position["entry_utc"]).timestamp())
            exit_reason: str | None = None
            if avg_funding <= 0.0:
                exit_reason = "FUNDING_DECAY"
            elif basis - float(position["entry_basis"]) >= BASIS_STOP_WIDENING:
                exit_reason = "BASIS_STOP"
            elif held_seconds >= MAX_HOLD_SECONDS:
                exit_reason = "MAX_HOLD"
            if exit_reason:
                close_position(state, spot_book, swap_book, exit_reason)

        if state.get("position") is None and signal_active:
            latest_event_ts = int(funding[-1]["timestamp_ms"])
            state["signal_count"] = int(state.get("signal_count") or 0) + 1
            state["position"] = {
                "entry_utc": now.isoformat(),
                "entry_timestamp_ms": int(now.timestamp() * 1000),
                "spot_entry_ask": spot_book["ask"],
                "swap_entry_bid": swap_book["bid"],
                "spot_quantity": LEG_NOTIONAL_USD / spot_book["ask"],
                "swap_quantity": LEG_NOTIONAL_USD / swap_book["bid"],
                "entry_basis": basis,
                "entry_avg_last3_funding": avg_funding,
                "last_processed_funding_timestamp_ms": latest_event_ts,
                "funding_pnl_usd": 0.0,
                "funding_events_collected": 0,
            }

        stats = trade_stats(state.get("trades") or [])
        review_checks = {
            "min_forward_trades": stats["closed_trades"] >= MIN_FORWARD_TRADES_FOR_REVIEW,
            "net_positive": finite(stats.get("net_return_percent")) and float(stats["net_return_percent"]) > 0,
            "profit_factor": finite(stats.get("profit_factor")) and float(stats["profit_factor"]) > 1.10,
            "drawdown": finite(stats.get("max_drawdown_percent")) and float(stats["max_drawdown_percent"]) <= 2.0,
            "no_data_error": True,
        }
        review_ready = all(review_checks.values())
        status = {
            "schema": SCHEMA,
            "updated_at_utc": now.isoformat(),
            "freeze_utc": state["freeze_utc"],
            "venue": "OKX",
            "strategy": "BTC_DELTA_NEUTRAL_FUNDING_CARRY_AVG3_GT_1_5BP",
            "cycle_count": state["cycle_count"],
            "signal_count": state["signal_count"],
            "current_market": {
                "spot_bid": spot_book["bid"],
                "spot_ask": spot_book["ask"],
                "spot_spread_bps": spot_book["spread_bps"],
                "swap_bid": swap_book["bid"],
                "swap_ask": swap_book["ask"],
                "swap_spread_bps": swap_book["spread_bps"],
                "basis": basis,
                "basis_bps": basis * 10_000.0,
                "last_three_realized_funding": latest_three,
                "average_last_three_funding": avg_funding,
                "average_last_three_funding_bps": avg_funding * 10_000.0,
                "entry_signal_active": signal_active,
            },
            "position": state.get("position"),
            "stats": stats,
            "recent_trades": (state.get("trades") or [])[-10:],
            "micro_live_review_checks": review_checks,
            "micro_live_review_ready": review_ready,
            "safety": safety,
            "private_api_used": False,
            "real_orders_enabled": False,
            "live_permission": False,
            "note": "Fresh OKX forward shadow. Historical candidate passed Binance+OKX holdout, but this file never grants LIVE permission.",
        }
        atomic_json(state_path, state)
        atomic_json(status_path, status)
        return status
    except Exception as exc:
        state["errors"] = (state.get("errors") or [])[-49:]
        state["errors"].append({"utc": now.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
        atomic_json(state_path, state)
        status = {
            "schema": SCHEMA,
            "updated_at_utc": now.isoformat(),
            "freeze_utc": state["freeze_utc"],
            "venue": "OKX",
            "strategy": "BTC_DELTA_NEUTRAL_FUNDING_CARRY_AVG3_GT_1_5BP",
            "state": "FAILED_SAFELY",
            "error": f"{type(exc).__name__}: {exc}",
            "position": state.get("position"),
            "stats": trade_stats(state.get("trades") or []),
            "micro_live_review_ready": False,
            "safety": safety,
            "private_api_used": False,
            "real_orders_enabled": False,
            "live_permission": False,
        }
        atomic_json(status_path, status)
        return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="funding_carry_forward_runtime")
    args = parser.parse_args()
    status = run_cycle(Path(args.state_dir))
    print(
        "FUNDING_FORWARD_OKX",
        "updated=", status.get("updated_at_utc"),
        "signal=", (status.get("current_market") or {}).get("entry_signal_active"),
        "position=", bool(status.get("position")),
        "closed=", (status.get("stats") or {}).get("closed_trades"),
        "review_ready=", status.get("micro_live_review_ready"),
        "real_orders=False",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
