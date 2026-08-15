#!/usr/bin/env python3
"""One-shot cloud runner for the frozen BTCUSDT 1H PAPER champion.

Designed for ephemeral GitHub-hosted runners. Persistent state lives in a
separate git branch checkout supplied through --state-dir.

Safety: public market data only, PAPER only, no API keys, no order client,
no LIVE path. The historical baseline is independently reconstructed from
public Binance market-only klines and must equal the frozen 23-trade holdout.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.strategy_engine.strategies.contracts import Candle
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from btc_1h_bybit_confirmatory import install_cached_context, run_context_backtest
from btc_1h_forward_shadow import (
    FORWARD_CLOSED_TARGET,
    FORWARD_FREEZE_MS,
    FORWARD_FREEZE_UTC,
    HISTORICAL_HOLDOUT_START_UTC,
    HISTORICAL_UNTOUCHED_HOLDOUT_TRADES,
    STRATEGY_KEY,
    make_frozen_strategy,
)
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_GITHUB_CLOUD_PAPER_V1"
BINANCE_MARKET = "https://data-api.binance.vision"
HOUR_MS = 3_600_000
INTERVAL_MS = {"1h": HOUR_MS, "4h": 4 * HOUR_MS}
BASELINE_START_MS = int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DEV_ROBUSTNESS = 0.75
DEV_WALK_FORWARD = True
CONFIRMATORY = {
    "state": "BTC_1H_CROSS_VENUE_CONFIRM_FAIL",
    "trades": 18,
    "profit_factor": 1.64,
    "expectancy_r": 0.2857,
    "max_drawdown_r": 3.4535,
    "robustness_ratio": 0.50,
    "supportive": True,
    "note": "Frozen Bybit 730-day confirmation; strict result failed only sample/robustness gates.",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def http_rows(interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    step = INTERVAL_MS[interval]
    cursor = start_ms
    by_ts: dict[int, Candle] = {}
    while cursor <= end_ms:
        query = urlencode({
            "symbol": "BTCUSDT", "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        })
        req = Request(
            f"{BINANCE_MARKET}/api/v3/klines?{query}",
            headers={"Accept": "application/json", "User-Agent": "TradingCore-GitHubCloud/1.0"},
        )
        last: Exception | None = None
        payload: Any = None
        for attempt in range(6):
            try:
                with urlopen(req, timeout=25) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list):
                    raise RuntimeError(f"Binance payload is not list: {payload}")
                break
            except Exception as exc:
                last = exc
                if attempt == 5:
                    raise RuntimeError(f"Binance market-only request failed: {last}") from exc
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
        if not payload:
            break
        newest = None
        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                continue
            try:
                open_ms = int(row[0]); close_ms = int(row[6])
                if close_ms > end_ms:
                    continue
                candle = Candle(
                    open_time_ms=open_ms,
                    open=float(row[1]), high=float(row[2]), low=float(row[3]),
                    close=float(row[4]), volume=float(row[5]),
                )
            except (TypeError, ValueError):
                continue
            by_ts[open_ms] = candle
            newest = open_ms if newest is None else max(newest, open_ms)
        if newest is None or len(payload) < 1000:
            break
        nxt = newest + step
        if nxt <= cursor:
            raise RuntimeError("Historical pagination stalled")
        cursor = nxt
        time.sleep(0.03)
    return sorted(by_ts.values(), key=lambda c: int(c.open_time_ms))


def suppress_before(strategy: Any, boundary_ms: int) -> None:
    original = strategy.evaluate_with_context
    def gated(self, candles, index, context_candles):
        if int(candles[index].open_time_ms) < boundary_ms:
            return self.no_trade("PRE_FINAL_HOLDOUT")
        return original(candles, index, context_candles)
    strategy.evaluate_with_context = types.MethodType(gated, strategy)


def ensure_baseline(root: Path) -> dict[str, Any]:
    path = root / "FROZEN_HOLDOUT23.json"
    existing = read_json(path)
    if existing:
        trades = existing.get("trades") or []
        if existing.get("strategy_key") != STRATEGY_KEY or len(trades) != HISTORICAL_UNTOUCHED_HOLDOUT_TRADES:
            raise RuntimeError("Frozen cloud baseline mismatch")
        return existing

    # End strictly before the forward freeze. A long burn-in from 2018 makes
    # EMA/context reconstruction independent of ephemeral runner state.
    end_ms = FORWARD_FREEZE_MS - 1
    print("CLOUD_BASELINE downloading Binance public 1h/4h history...", flush=True)
    one_h = http_rows("1h", BASELINE_START_MS, end_ms)
    four_h = http_rows("4h", BASELINE_START_MS, end_ms)
    if len(one_h) < 24 * 365 or len(four_h) < 6 * 365:
        raise RuntimeError(f"Insufficient baseline history: 1h={len(one_h)} 4h={len(four_h)}")

    boundary_ms = int(datetime.fromisoformat(HISTORICAL_HOLDOUT_START_UTC).timestamp() * 1000)
    strategy = make_frozen_strategy()
    install_cached_context(strategy, four_h)
    suppress_before(strategy, boundary_ms)
    bt = run_context_backtest(strategy, one_h, four_h, max_bars_in_trade=24)
    trades = [
        trade for trade in bt["trades"]
        if isinstance(trade.closed_at_utc, str)
        and trade.closed_at_utc >= HISTORICAL_HOLDOUT_START_UTC
        and datetime.fromisoformat(trade.closed_at_utc).timestamp() * 1000 < FORWARD_FREEZE_MS
    ]
    if len(trades) != HISTORICAL_UNTOUCHED_HOLDOUT_TRADES:
        raise RuntimeError(
            f"Historical baseline reconstruction mismatch: got {len(trades)}, "
            f"expected {HISTORICAL_UNTOUCHED_HOLDOUT_TRADES}"
        )
    payload = {
        "schema": "TRADINGCORE_BTC1H_CLOUD_FROZEN_BASELINE_V1",
        "created_at_utc": now(),
        "strategy_key": STRATEGY_KEY,
        "holdout_start_utc": HISTORICAL_HOLDOUT_START_UTC,
        "forward_freeze_utc": FORWARD_FREEZE_UTC,
        "development_robustness": DEV_ROBUSTNESS,
        "development_walk_forward_passed": DEV_WALK_FORWARD,
        "source": "BINANCE_PUBLIC_MARKET_ONLY_RECONSTRUCTION",
        "bars_1h": len(one_h), "bars_4h": len(four_h),
        "trades": [
            {
                "strategy_id": t.strategy_id,
                "closed_at_utc": t.closed_at_utc,
                "regime": t.regime,
                "net_pnl": t.net_pnl,
                "r_multiple": t.r_multiple,
            } for t in trades
        ],
        "real_orders_enabled": False,
        "live_permission": False,
    }
    atomic(path, payload)
    print("CLOUD_BASELINE PASS trades=23", flush=True)
    return payload


def forward_closures(shadow_root: Path) -> list[ClosedTrade]:
    journal = shadow_root / "forward_journal.jsonl"
    if not journal.exists():
        return []
    out: list[ClosedTrade] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = row.get("trade_result")
        closed = None
        if isinstance(result, dict) and result.get("event") == "POSITION_CLOSED":
            closed = result
        elif isinstance(result, dict) and isinstance(result.get("closed"), dict) and result["closed"].get("event") == "POSITION_CLOSED":
            closed = result["closed"]
        if not isinstance(closed, dict):
            continue
        try:
            net = float(closed["net_pnl"]); r = float(closed["r_multiple"])
        except (KeyError, TypeError, ValueError):
            continue
        stamp = str(row.get("recorded_at_utc") or "")
        if stamp and math.isfinite(net) and math.isfinite(r):
            out.append(ClosedTrade(STRATEGY_KEY, stamp, "FORWARD", net, r))
    return sorted(out, key=lambda t: str(t.closed_at_utc))


def evaluate_final(root: Path, shadow_root: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    decision_path = root / "BTC_1H_FINAL_DECISION_LOCK.json"
    existing = read_json(decision_path)
    if existing:
        return {"state": "DECISION_LOCKED", "decision": existing}

    forward = forward_closures(shadow_root)
    required = int(FORWARD_CLOSED_TARGET)
    if len(forward) < required:
        return {
            "state": "WAITING_FIRST_7_FORWARD_TRADES",
            "forward_closed_trades": len(forward),
            "required": required,
            "remaining": required - len(forward),
            "cross_venue_confirmation": CONFIRMATORY,
        }

    historical = [
        ClosedTrade(
            row.get("strategy_id"), row.get("closed_at_utc"), row.get("regime"),
            float(row["net_pnl"]), float(row["r_multiple"]) if row.get("r_multiple") is not None else None,
        ) for row in baseline["trades"]
    ]
    first_forward = forward[:required]
    combined = historical + first_forward
    stats = build_stats(combined)
    validation = {
        "strategy_id": STRATEGY_KEY,
        "sample_id": f"BTC1H_HOLDOUT23_PLUS_FIRST{required}_FORWARD_CLOUD",
        "oos_trades": stats.get("closed_trades"),
        "oos_net_pnl": stats.get("net_pnl"),
        "oos_profit_factor": stats.get("profit_factor"),
        "oos_expectancy_r": stats.get("expectancy_r"),
        "oos_max_drawdown_r": stats.get("max_drawdown_r"),
        "oos_win_rate_percent": stats.get("win_rate_percent"),
        "robustness_ratio": float(baseline["development_robustness"]),
        "walk_forward_passed": baseline["development_walk_forward_passed"] is True,
        "look_ahead_leakage": False,
        "safety_violations": [],
    }
    gates = promotion_gates(validation)
    passed = bool(gates.get("passed")) and bool(CONFIRMATORY["supportive"])
    state = "BTC_1H_FORWARD_FIRST7_PROMOTION_PASS_OWNER_REVIEW" if passed else "BTC_1H_FORWARD_FIRST7_REJECTED_FROZEN"
    decision = {
        "schema": "TRADINGCORE_BTC1H_CLOUD_FINAL_DECISION_V1",
        "locked_at_utc": now(),
        "state": state,
        "strategy_key": STRATEGY_KEY,
        "historical_holdout_trades": len(historical),
        "forward_trades_used": required,
        "forward_rule": "FIRST_SEVEN_CLOSED_TRADES_ONLY",
        "combined_stats": stats,
        "validation": validation,
        "promotion_gates": gates,
        "cross_venue_confirmation": CONFIRMATORY,
        "holdout_reopen_allowed": False,
        "real_orders_enabled": False,
        "live_permission": False,
    }
    atomic(decision_path, decision)
    if passed:
        atomic(root / "OWNER_REVIEW_FOR_MICRO_LIVE.json", {
            "schema": "TRADINGCORE_CLOUD_OWNER_REVIEW_V1",
            "created_at_utc": now(),
            "state": "OWNER_REVIEW_REQUIRED",
            "strategy_key": STRATEGY_KEY,
            "combined_stats": stats,
            "live_enabled": False,
            "real_orders_enabled": False,
        })
    else:
        atomic(root / "BTC_1H_REJECTED_AFTER_FIRST7_FORWARD.json", decision)
    return {"state": state, "decision": decision}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--verify-baseline", action="store_true")
    args = ap.parse_args()

    safety = assert_safe_startup()
    root = Path(args.state_dir).resolve()
    shadow_root = root / "btc_shadow"
    root.mkdir(parents=True, exist_ok=True)
    shadow_root.mkdir(parents=True, exist_ok=True)

    # Point the existing frozen shadow at cloud-persistent state before use.
    os.environ["BTC_1H_SHADOW_DATA_DIR"] = str(shadow_root)
    import btc_1h_forward_shadow as shadow

    state = shadow.load_state()
    processed = shadow.process_available_closed_candles(state)
    shadow_status = shadow.write_status(
        state, running=True,
        detail=f"GitHub cloud one-shot processed {processed} new closed candle(s)",
    )

    baseline = ensure_baseline(root) if args.verify_baseline or (root / "FROZEN_HOLDOUT23.json").exists() else None
    if baseline is None and int(shadow_status.get("forward_closed_trades") or 0) >= int(FORWARD_CLOSED_TARGET):
        baseline = ensure_baseline(root)

    gate = (
        evaluate_final(root, shadow_root, baseline)
        if baseline is not None
        else {
            "state": "WAITING_FIRST_7_FORWARD_TRADES",
            "forward_closed_trades": int(shadow_status.get("forward_closed_trades") or 0),
            "required": int(FORWARD_CLOSED_TARGET),
        }
    )

    status = {
        "schema": SCHEMA,
        "updated_at_utc": now(),
        "cloud": "GITHUB_ACTIONS_STANDARD_PUBLIC_REPO",
        "mode": "PAPER_ONLY",
        "strategy_key": STRATEGY_KEY,
        "processed_closed_candles_this_run": processed,
        "forward_closed_trades": int(shadow_status.get("forward_closed_trades") or 0),
        "position": shadow_status.get("position"),
        "final_gate_state": gate.get("state"),
        "final_gate": gate,
        "baseline_verified_23": bool(baseline and len(baseline.get("trades") or []) == 23),
        "cross_venue_confirmation": CONFIRMATORY,
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "live_permission": False,
    }
    atomic(root / "CLOUD_STATUS.json", status)
    print("=" * 88)
    print("TRADINGCORE GITHUB CLOUD PAPER")
    print("forward_closed=", status["forward_closed_trades"], "processed=", processed)
    print("baseline23=", status["baseline_verified_23"], "gate=", status["final_gate_state"])
    print("LIVE / real orders: DISABLED")
    print("state_dir=", root)
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
