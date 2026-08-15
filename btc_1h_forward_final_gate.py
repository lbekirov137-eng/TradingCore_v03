#!/usr/bin/env python3
"""Frozen final gate for the BTCUSDT 1H PAPER champion.

This module does not search, tune, or trade. It waits for exactly the FIRST seven
new forward-shadow closed trades after the 2026-08-14 freeze, reconstructs the
already-frozen 23-trade historical final holdout from the exact Strategy Research
Team dataset, and evaluates the combined 30-trade sample once.

The first eligible decision is locked permanently. A PASS only creates an owner
review marker; LIVE and real orders remain disabled and require separate execution
architecture plus explicit owner approval.
"""
from __future__ import annotations

import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.strategy_engine.strategies.contracts import Candle
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from btc_1h_bybit_confirmatory import install_cached_context, run_context_backtest
from btc_1h_forward_shadow import (
    FORWARD_CLOSED_TARGET,
    HISTORICAL_HOLDOUT_START_UTC,
    HISTORICAL_UNTOUCHED_HOLDOUT_TRADES,
    STRATEGY_KEY,
    make_frozen_strategy,
)
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_BTC_1H_FORWARD_FINAL_GATE_V1"
REPO = Path(__file__).resolve().parent
RESEARCH_ROOT = REPO / "strategy_research_results"
SHADOW_ROOT = Path("C:/TradingCore_BTC_1H_SHADOW")
STABLE_ROOT = Path("C:/TradingCore_Stable_Paper")
STATUS_PATH = STABLE_ROOT / "BTC_1H_FINAL_GATE_STATUS.json"
DECISION_PATH = STABLE_ROOT / "BTC_1H_FINAL_DECISION_LOCK.json"
OWNER_REVIEW_PATH = STABLE_ROOT / "OWNER_REVIEW_FOR_MICRO_LIVE.json"
REJECT_PATH = STABLE_ROOT / "BTC_1H_REJECTED_AFTER_FIRST7_FORWARD.json"
CONFIRMATORY = Path("C:/TradingCore_BTC_1H_CONFIRMATORY/LATEST_BTC_1H_BYBIT_CONFIRMATORY.json")


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


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def load_frozen_research() -> dict[str, Any]:
    candidates = sorted(RESEARCH_ROOT.glob("timeframe_research_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        report = read_json(path)
        if not report:
            continue
        if report.get("chosen_timeframe") != "1h":
            continue
        if str(report.get("chosen_strategy_key") or "") != STRATEGY_KEY:
            continue
        if str(report.get("holdout_start_utc") or "") != HISTORICAL_HOLDOUT_START_UTC:
            continue

        dev = next((x for x in report.get("development_results") or [] if x.get("timeframe") == "1h"), None)
        if not isinstance(dev, dict):
            continue
        robust = dev.get("development_robustness")
        wf = dev.get("development_walk_forward_passed")
        if not isinstance(robust, (int, float)) or not math.isfinite(float(robust)) or wf is not True:
            continue

        datasets: dict[str, Path] = {}
        for row in report.get("downloads") or []:
            if not isinstance(row, dict):
                continue
            interval = str(row.get("interval") or "")
            raw = row.get("path")
            if interval in ("1h", "4h") and raw:
                datasets[interval] = resolve_path(str(raw))
        if "1h" not in datasets or "4h" not in datasets:
            continue
        if not datasets["1h"].exists() or not datasets["4h"].exists():
            continue

        return {
            "report_path": str(path),
            "development_robustness": float(robust),
            "development_walk_forward_passed": True,
            "dataset_1h": datasets["1h"],
            "dataset_4h": datasets["4h"],
        }
    raise RuntimeError("Exact frozen BTC 1H research report/datasets not found")


def load_dataset(path: Path) -> list[Candle]:
    payload = read_json(path)
    if not payload:
        raise RuntimeError(f"Dataset unreadable: {path}")
    arrays = [payload.get("timestamps"), payload.get("opens"), payload.get("highs"), payload.get("lows"), payload.get("closes"), payload.get("volumes")]
    if not all(isinstance(x, list) for x in arrays):
        raise RuntimeError(f"Dataset schema mismatch: {path}")
    size = min(len(x) for x in arrays)
    rows: list[Candle] = []
    for i in range(size):
        rows.append(Candle(
            open_time_ms=int(arrays[0][i]), open=float(arrays[1][i]), high=float(arrays[2][i]),
            low=float(arrays[3][i]), close=float(arrays[4][i]), volume=float(arrays[5][i]),
        ))
    return rows


def suppress_before(strategy: Any, boundary_ms: int) -> None:
    original = strategy.evaluate_with_context

    def gated(self, candles, index, context_candles):
        if int(candles[index].open_time_ms) < boundary_ms:
            return self.no_trade("PRE_FINAL_HOLDOUT")
        return original(candles, index, context_candles)

    strategy.evaluate_with_context = types.MethodType(gated, strategy)


def reconstruct_historical_holdout(evidence: dict[str, Any]) -> list[ClosedTrade]:
    one_h = load_dataset(Path(evidence["dataset_1h"]))
    four_h = load_dataset(Path(evidence["dataset_4h"]))
    boundary_ms = int(datetime.fromisoformat(HISTORICAL_HOLDOUT_START_UTC).timestamp() * 1000)
    strategy = make_frozen_strategy()
    install_cached_context(strategy, four_h)
    suppress_before(strategy, boundary_ms)
    bt = run_context_backtest(strategy, one_h, four_h, max_bars_in_trade=24)
    trades = [
        t for t in bt["trades"]
        if isinstance(t.closed_at_utc, str) and t.closed_at_utc >= HISTORICAL_HOLDOUT_START_UTC
    ]
    if len(trades) != HISTORICAL_UNTOUCHED_HOLDOUT_TRADES:
        raise RuntimeError(
            f"Historical holdout reconstruction mismatch: got {len(trades)}, "
            f"expected {HISTORICAL_UNTOUCHED_HOLDOUT_TRADES}"
        )
    return trades


def forward_closures() -> list[ClosedTrade]:
    journal = SHADOW_ROOT / "forward_journal.jsonl"
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
            net = float(closed["net_pnl"])
            r = float(closed["r_multiple"])
        except (KeyError, TypeError, ValueError):
            continue
        stamp = str(row.get("recorded_at_utc") or "")
        if not stamp or not math.isfinite(net) or not math.isfinite(r):
            continue
        out.append(ClosedTrade(STRATEGY_KEY, stamp, "FORWARD", net, r))
    return sorted(out, key=lambda t: str(t.closed_at_utc))


def confirmatory_economics() -> dict[str, Any]:
    report = read_json(CONFIRMATORY) or {}
    validation = report.get("validation") or {}
    pf = validation.get("oos_profit_factor")
    exp = validation.get("oos_expectancy_r")
    dd = validation.get("oos_max_drawdown_r")
    supportive = (
        isinstance(pf, (int, float)) and float(pf) >= 1.15
        and isinstance(exp, (int, float)) and float(exp) > 0
        and isinstance(dd, (int, float)) and float(dd) <= 10.0
    )
    return {
        "supportive": supportive,
        "state": report.get("state"),
        "trades": validation.get("oos_trades"),
        "profit_factor": pf,
        "expectancy_r": exp,
        "max_drawdown_r": dd,
        "robustness_ratio": validation.get("robustness_ratio"),
    }


def main() -> int:
    safety = assert_safe_startup()
    STABLE_ROOT.mkdir(parents=True, exist_ok=True)

    existing = read_json(DECISION_PATH)
    if existing:
        atomic(STATUS_PATH, {
            "schema": SCHEMA, "state": "DECISION_LOCKED", "decision": existing,
            "updated_at_utc": now(), "real_orders_enabled": False, "live_permission": False,
        })
        print("BTC1H_FINAL_GATE decision already locked:", existing.get("state"), flush=True)
        return 0

    shadow_status = read_json(SHADOW_ROOT / "status.json") or {}
    if shadow_status.get("real_orders_enabled") is not False:
        raise RuntimeError("Unsafe/missing BTC shadow real_orders_enabled=false marker")

    forward = forward_closures()
    required = int(FORWARD_CLOSED_TARGET)
    confirm = confirmatory_economics()

    if len(forward) < required:
        status = {
            "schema": SCHEMA,
            "state": "WAITING_FIRST_7_FORWARD_TRADES",
            "updated_at_utc": now(),
            "historical_holdout_trades": HISTORICAL_UNTOUCHED_HOLDOUT_TRADES,
            "forward_closed_trades": len(forward),
            "forward_required_for_first_decision": required,
            "remaining": required - len(forward),
            "cross_venue_confirmation": confirm,
            "safety": safety,
            "real_orders_enabled": False,
            "live_permission": False,
        }
        atomic(STATUS_PATH, status)
        print(
            f"BTC1H_FINAL_GATE waiting: forward={len(forward)}/{required} "
            f"cross_venue_supportive={confirm['supportive']} real_orders=False",
            flush=True,
        )
        return 0

    evidence = load_frozen_research()
    historical = reconstruct_historical_holdout(evidence)
    first_forward = forward[:required]
    combined = historical + first_forward
    stats = build_stats(combined)
    validation = {
        "strategy_id": STRATEGY_KEY,
        "sample_id": f"BTC1H_HOLDOUT23_PLUS_FIRST{required}_FORWARD",
        "oos_trades": stats.get("closed_trades"),
        "oos_net_pnl": stats.get("net_pnl"),
        "oos_profit_factor": stats.get("profit_factor"),
        "oos_expectancy_r": stats.get("expectancy_r"),
        "oos_max_drawdown_r": stats.get("max_drawdown_r"),
        "oos_win_rate_percent": stats.get("win_rate_percent"),
        "robustness_ratio": evidence["development_robustness"],
        "walk_forward_passed": evidence["development_walk_forward_passed"],
        "look_ahead_leakage": False,
        "safety_violations": [],
    }
    gates = promotion_gates(validation)
    passed = bool(gates.get("passed")) and bool(confirm.get("supportive"))
    decision_state = (
        "BTC_1H_FORWARD_FIRST7_PROMOTION_PASS_OWNER_REVIEW"
        if passed else "BTC_1H_FORWARD_FIRST7_REJECTED_FROZEN"
    )
    decision = {
        "schema": "TRADINGCORE_BTC_1H_FORWARD_FINAL_DECISION_V1",
        "locked_at_utc": now(),
        "state": decision_state,
        "strategy_key": STRATEGY_KEY,
        "historical_holdout_trades": len(historical),
        "forward_trades_used": required,
        "forward_rule": "FIRST_SEVEN_CLOSED_TRADES_ONLY",
        "combined_stats": stats,
        "validation": validation,
        "promotion_gates": gates,
        "cross_venue_confirmation": confirm,
        "research_report": evidence["report_path"],
        "holdout_reopen_allowed": False,
        "real_orders_enabled": False,
        "live_permission": False,
    }
    atomic(DECISION_PATH, decision)
    atomic(STATUS_PATH, {"schema": SCHEMA, "state": decision_state, "decision": decision, "updated_at_utc": now(), "real_orders_enabled": False, "live_permission": False})

    if passed:
        atomic(OWNER_REVIEW_PATH, {
            "schema": "TRADINGCORE_OWNER_MICRO_LIVE_REVIEW_V1",
            "created_at_utc": now(),
            "state": "OWNER_REVIEW_REQUIRED",
            "strategy_key": STRATEGY_KEY,
            "combined_stats": stats,
            "cross_venue_confirmation": confirm,
            "live_enabled": False,
            "real_orders_enabled": False,
            "note": "Research + first-seven forward gate passed. LIVE remains disabled; separate micro-live execution design and explicit owner approval are still required.",
        })
    else:
        atomic(REJECT_PATH, decision)

    print("=" * 88)
    print("BTC 1H FIRST-SEVEN FORWARD FINAL DECISION")
    print("State:", decision_state)
    print("Combined trades:", stats.get("closed_trades"), "PF=", stats.get("profit_factor"), "expR=", stats.get("expectancy_r"), "DD_R=", stats.get("max_drawdown_r"))
    print("Promotion gates:", gates.get("passed"), "failed=", gates.get("failed_gates"))
    print("Cross-venue economics supportive:", confirm.get("supportive"))
    print("LIVE / real orders: DISABLED")
    print("Decision:", DECISION_PATH)
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
