#!/usr/bin/env python3
"""
Collector B cloud supervisor.

Runs ONLY research/data-collection components:
- Collector B Bybit public all-liquidation collector continuously;
- G2/G3 read-only data-quality audit hourly;
- readiness marker only after G2 PASS + G3 PRELIMINARY PASS.

No order client, no account API, no strategy/outcome research, no LIVE path.
Collector A is not accessed or modified.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("COLLECTOR_B_DATA_DIR", "/data"))
AUDIT_DIR = DATA / "audit"
READY = DATA / "READY_FOR_OUTCOME_RESEARCH.json"
SUPERVISOR = DATA / "cloud_supervisor_status.json"
AUDIT_INTERVAL_SECONDS = int(os.getenv("COLLECTOR_B_AUDIT_INTERVAL_SECONDS", "3600"))
AUDIT_INITIAL_DELAY_SECONDS = int(os.getenv("COLLECTOR_B_AUDIT_INITIAL_DELAY_SECONDS", "60"))

_stop = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def request_stop(*_: Any) -> None:
    global _stop
    _stop = True


def write_status(**kwargs: Any) -> None:
    payload = {
        "schema": "TRADINGCORE_COLLECTOR_B_CLOUD_SUPERVISOR_V1",
        "mode": "RESEARCH_ONLY",
        "collector_a_modified": False,
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "updated_at_utc": utc_now(),
        **kwargs,
    }
    atomic_json(SUPERVISOR, payload)


def run_audit() -> dict[str, Any] | None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "collector_b_g2_g3_audit.py"),
        "--data-dir",
        str(DATA),
        "--output",
        str(AUDIT_DIR),
    ]
    print("[CLOUD SUPERVISOR] running G2/G3 audit", flush=True)
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    latest = AUDIT_DIR / "LATEST_COLLECTOR_B_G2_G3.json"
    if completed.returncode != 0 or not latest.exists():
        write_status(state="AUDIT_FAILED_SAFE", audit_returncode=completed.returncode)
        return None

    try:
        report = json.loads(latest.read_text(encoding="utf-8"))
    except Exception as error:
        write_status(state="AUDIT_RESULT_INVALID", error=f"{type(error).__name__}: {error}")
        return None

    g2 = str((report.get("g2") or {}).get("state"))
    g3 = str((report.get("g3") or {}).get("state"))
    evidence = report.get("evidence") or {}
    state = "WAITING_FOR_DATA"

    if g2 == "G2_PASS" and g3 == "G3_PRELIMINARY_PASS":
        state = "READY_FOR_OUTCOME_RESEARCH"
        atomic_json(
            READY,
            {
                "schema": "TRADINGCORE_COLLECTOR_B_READY_V1",
                "state": state,
                "g2": g2,
                "g3": g3,
                "events": evidence.get("valid_unique_events"),
                "observation_span_hours": evidence.get("observation_span_hours"),
                "generated_at_utc": report.get("generated_at_utc"),
                "collector_a_modified": False,
                "real_orders_enabled": False,
                "live_trading": False,
                "note": "Data-quality readiness only. Not evidence of edge/profitability and not LIVE permission.",
            },
        )
    elif g2 == "G2_REPAIR_REQUIRED" or g3 == "G3_REPAIR_REQUIRED":
        state = "ATTENTION_REQUIRED"

    write_status(
        state=state,
        g2=g2,
        g3=g3,
        events=evidence.get("valid_unique_events"),
        observation_span_hours=evidence.get("observation_span_hours"),
        last_audit_utc=utc_now(),
    )
    return report


def main() -> int:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass

    DATA.mkdir(parents=True, exist_ok=True)
    write_status(state="STARTING")

    collector: subprocess.Popen[Any] | None = None
    next_audit = time.monotonic() + max(5, AUDIT_INITIAL_DELAY_SECONDS)
    restart_backoff = 1.0

    try:
        while not _stop:
            if collector is None or collector.poll() is not None:
                if collector is not None:
                    print(f"[CLOUD SUPERVISOR] collector exited rc={collector.returncode}; restarting", flush=True)
                    time.sleep(restart_backoff)
                    restart_backoff = min(restart_backoff * 2.0, 30.0)
                collector = subprocess.Popen(
                    [sys.executable, str(ROOT / "collector_b_bybit.py")],
                    cwd=str(ROOT),
                    env={**os.environ, "COLLECTOR_B_DATA_DIR": str(DATA)},
                )
                restart_backoff = 1.0
                write_status(state="COLLECTOR_RUNNING", collector_pid=collector.pid)

            if time.monotonic() >= next_audit:
                run_audit()
                next_audit = time.monotonic() + max(300, AUDIT_INTERVAL_SECONDS)

            time.sleep(2.0)
    finally:
        if collector is not None and collector.poll() is None:
            collector.terminate()
            try:
                collector.wait(timeout=20)
            except subprocess.TimeoutExpired:
                collector.kill()
                collector.wait(timeout=5)
        write_status(state="STOPPED")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
