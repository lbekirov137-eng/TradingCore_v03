#!/usr/bin/env python3
"""Autonomous state machine for Collector B -> research -> forward PAPER.

The orchestrator is deliberately incapable of LIVE trading. It advances only
through predeclared gates and locks a historical decision so a failed holdout
cannot be repeatedly re-opened on an expanded sample.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.startup_safety import assert_safe_startup
import forced_flow_protocol as protocol

SCHEMA = "TRADINGCORE_FORCED_FLOW_AUTONOMOUS_ORCHESTRATOR_V1"


def utc_now() -> str:
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


def run_script(python: str, script: Path, args: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== {utc_now()} RUN {script.name} ===\n")
        completed = subprocess.run(
            [python, str(script), *args],
            cwd=str(script.parent),
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
            env={
                **os.environ,
                "TRADING_ENVIRONMENT": "PAPER",
                "LIVE_TRADING": "false",
                "PAPER_TRADING": "true",
                "DEMO_ONLY": "true",
            },
        )
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="C:/TradingCore_Collector_B/data")
    parser.add_argument("--state-dir", default="C:/TradingCore_Autonomous")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--interval-seconds", type=int, default=900)
    args = parser.parse_args()

    safety = assert_safe_startup()
    root = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    logs = state_dir / "logs"
    status_path = state_dir / "status.json"
    decision_lock = state_dir / "historical_decision_lock.json"
    forward_marker = state_dir / "FORWARD_PAPER_AUTHORIZED_BY_RESEARCH.json"

    audit_output = root / "collector_b_audit_results"
    research_output = root / "forced_flow_research_results"

    print("=" * 88)
    print("TRADINGCORE FORCED-FLOW AUTONOMOUS ORCHESTRATOR")
    print("PAPER/RESEARCH ONLY | REAL ORDERS IMPOSSIBLE")
    print("Protocol:", protocol.PROTOCOL_VERSION, protocol.PROTOCOL_FINGERPRINT)
    print("=" * 88, flush=True)

    while True:
        state: dict[str, Any] = {
            "schema": SCHEMA,
            "updated_at_utc": utc_now(),
            "safety": safety,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
            "collector_a_modified": False,
            "private_api_used": False,
            "real_orders_enabled": False,
            "real_order_sent": False,
        }

        # A historical decision is immutable for this protocol/sample. Never
        # reopen a failed holdout merely because more observations arrived.
        locked = read_json(decision_lock)
        if locked:
            decision = str(locked.get("decision") or "UNKNOWN")
            state["state"] = (
                "HISTORICAL_PASS_FORWARD_PAPER"
                if decision == "HISTORICAL_PROMOTION_PASS"
                else "PROTOCOL_V1_REJECTED_FROZEN"
            )
            state["historical_decision_lock"] = locked
            state["forward_paper_authorized"] = forward_marker.exists()
            atomic_json(status_path, state)
            time.sleep(max(300, args.interval_seconds))
            continue

        # Always refresh read-only data quality first.
        audit_rc = run_script(
            args.python,
            root / "collector_b_g2_g3_audit.py",
            ["--data-dir", str(data_dir)],
            logs / "g2_g3.log",
        )
        audit = read_json(audit_output / "LATEST_COLLECTOR_B_G2_G3.json")
        if audit_rc != 0 or not audit:
            state.update(state="AUDIT_FAILED_SAFE", audit_returncode=audit_rc)
            atomic_json(status_path, state)
            time.sleep(max(300, args.interval_seconds))
            continue

        g2 = str((audit.get("g2") or {}).get("state"))
        g3 = str((audit.get("g3") or {}).get("state"))
        state.update(
            g2=g2,
            g3=g3,
            valid_events=(audit.get("evidence") or {}).get("valid_unique_events"),
            observation_span_hours=(audit.get("evidence") or {}).get("observation_span_hours"),
        )

        if g2 != "G2_PASS" or g3 != "G3_PRELIMINARY_PASS":
            state["state"] = "WAITING_FOR_DATA_QUALITY_GATES"
            atomic_json(status_path, state)
            time.sleep(max(300, args.interval_seconds))
            continue

        # Data quality passed. The research engine has its own stronger,
        # preregistered evidence-volume gate (1000 events / 72h / 100 clusters).
        research_rc = run_script(
            args.python,
            root / "forced_flow_research_engine.py",
            ["--data-dir", str(data_dir)],
            logs / "forced_flow_research.log",
        )
        research = read_json(research_output / "LATEST_FORCED_FLOW_RESEARCH.json")
        if research_rc != 0 or not research:
            state.update(state="RESEARCH_FAILED_SAFE", research_returncode=research_rc)
            atomic_json(status_path, state)
            time.sleep(max(300, args.interval_seconds))
            continue

        research_state = str(research.get("state") or "UNKNOWN")
        state["research_state"] = research_state
        state["research_event_count"] = research.get("event_count")
        state["primary_long_clusters"] = research.get("primary_long_clusters")
        state["readiness_missing"] = research.get("readiness_missing")

        if research_state in (
            "WAITING_FOR_PREREGISTERED_SAMPLE",
            "INSUFFICIENT_TRADE_GEOMETRY",
        ):
            state["state"] = "WAITING_FOR_PREREGISTERED_RESEARCH_SAMPLE"
            atomic_json(status_path, state)
            time.sleep(max(300, args.interval_seconds))
            continue

        if research_state not in (
            "HISTORICAL_PROMOTION_PASS",
            "HISTORICAL_REJECT_OR_MORE_DATA",
        ):
            state["state"] = "RESEARCH_STATE_UNRECOGNISED_FAIL_SAFE"
            atomic_json(status_path, state)
            time.sleep(max(300, args.interval_seconds))
            continue

        # Freeze the first final-holdout decision forever for protocol V1.
        lock = {
            "schema": "TRADINGCORE_FORCED_FLOW_HISTORICAL_DECISION_LOCK_V1",
            "locked_at_utc": utc_now(),
            "protocol_version": protocol.PROTOCOL_VERSION,
            "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
            "decision": research_state,
            "sample_id": research.get("sample_id"),
            "promotion_gates": research.get("promotion_gates"),
            "validation": research.get("validation"),
            "event_count": research.get("event_count"),
            "primary_long_clusters": research.get("primary_long_clusters"),
            "collector_a_modified": False,
            "real_orders_enabled": False,
        }
        atomic_json(decision_lock, lock)

        if research_state == "HISTORICAL_PROMOTION_PASS":
            marker = {
                "schema": "TRADINGCORE_FORCED_FLOW_FORWARD_PAPER_AUTH_V1",
                "authorized_at_utc": utc_now(),
                "protocol_version": protocol.PROTOCOL_VERSION,
                "protocol_fingerprint": protocol.PROTOCOL_FINGERPRINT,
                "sample_id": research.get("sample_id"),
                "mode": "PAPER_ONLY",
                "real_orders_enabled": False,
                "live_permission": False,
                "required_forward_closed_trades": protocol.FORWARD_PAPER_MIN_CLOSED_TRADES,
                "note": "Historical research passed. This authorizes FORWARD PAPER only; never LIVE.",
            }
            atomic_json(forward_marker, marker)
            state["state"] = "HISTORICAL_PASS_FORWARD_PAPER_AUTHORIZED"
            state["forward_paper_authorized"] = True
        else:
            state["state"] = "PROTOCOL_V1_REJECTED_FROZEN"
            state["forward_paper_authorized"] = False

        atomic_json(status_path, state)
        time.sleep(max(300, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
