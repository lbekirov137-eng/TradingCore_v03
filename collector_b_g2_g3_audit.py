#!/usr/bin/env python3
"""
TradingCore Collector B — G2/G3 data quality audit.

READ-ONLY AUDIT ONLY.

This audit does not modify Collector A or Collector B evidence, does not compute
trading outcomes, does not search strategies, and has no order path.

G2 checks:
- startup safety remains PAPER / no LIVE;
- Collector B status identity and hard safety flags;
- public Bybit subscription acknowledgement for the frozen allLiquidation topics;
- public instrument metadata for BTCUSDT / ETHUSDT / SOLUSDT linear contracts;
- normalized schema, side semantics, positive numeric size/price and timestamps.

G3 preliminary checks:
- duplicate event keys;
- source/event/receive timestamp ordering and latency sanity;
- reconnect count / local gap-accounting requirement;
- minimum observation sample before a preliminary data-quality pass.

The audit derives quote_notional_usdt = executed_size * bankruptcy_price only for
QA summaries. It never writes that value into collected evidence and never calls
it USD. Stablecoin/USD conversion remains a separate downstream gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect

from config.startup_safety import assert_safe_startup


SCHEMA = "TRADINGCORE_COLLECTOR_B_G2_G3_AUDIT_V1"
COLLECTOR_SCHEMA = "TRADINGCORE_COLLECTOR_B_BYBIT_V1"
COLLECTOR_ID = "COLLECTOR_B_BYBIT_PUBLIC_ALL_LIQUIDATION"
PUBLIC_WS_URL = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_REST_BASE = "https://api.bybit.com"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TOPICS = tuple(f"allLiquidation.{symbol}" for symbol in SYMBOLS)
EXPECTED_SIDE_MAP = {"Buy": "LONG", "Sell": "SHORT"}

# This is only a PRELIMINARY local-capture QA threshold, not an outcome or
# profitability threshold. Final G3 completeness remains open until enough
# observation time exists and any reconnect gaps are reconciled.
G3_MIN_EVENTS = 100
G3_MIN_SPAN_HOURS = 6.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected object in {path}")
    return payload


def _http_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode(params)
    request = Request(
        f"{PUBLIC_REST_BASE}{path}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "TradingCore-CollectorB-G2G3-Audit/1.0",
        },
        method="GET",
    )
    with urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise TypeError("Bybit public REST response must be an object")
    return payload


def fetch_instrument(symbol: str) -> dict[str, Any]:
    payload = _http_json(
        "/v5/market/instruments-info",
        {"category": "linear", "symbol": symbol, "limit": 1},
    )
    if int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit instrument info failed for {symbol}: {payload.get('retMsg')}")
    result = payload.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"No Bybit instrument metadata for {symbol}")
    row = rows[0]
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid instrument metadata row for {symbol}")
    return row


async def subscription_probe(timeout_seconds: float = 12.0) -> dict[str, Any]:
    req_id = f"collector-b-g2-{int(time.time() * 1000)}"
    async with connect(
        PUBLIC_WS_URL,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=1_000_000,
    ) as websocket:
        await websocket.send(json.dumps({"req_id": req_id, "op": "subscribe", "args": list(TOPICS)}))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            if payload.get("op") == "subscribe":
                return {
                    "ack_received": True,
                    "success": payload.get("success") is True,
                    "ret_msg": payload.get("ret_msg"),
                    "conn_id": payload.get("conn_id"),
                    "req_id": payload.get("req_id"),
                    "raw": payload,
                }
        return {"ack_received": False, "success": False, "reason": "SUBSCRIBE_ACK_TIMEOUT"}


def scan_evidence(data_dir: Path) -> dict[str, Any]:
    normalized_root = data_dir / "normalized" / "bybit"
    files = sorted(normalized_root.glob("*.jsonl")) if normalized_root.exists() else []

    total_lines = 0
    invalid_json = 0
    invalid_schema = 0
    invalid_symbol = 0
    invalid_side = 0
    invalid_numeric = 0
    invalid_timestamp = 0
    duplicate_keys = 0
    timestamp_order_anomalies = 0
    keys: set[str] = set()
    event_times: list[int] = []
    source_event_ms: list[float] = []
    receive_source_ms: list[float] = []
    quote_notional_usdt: list[float] = []
    symbol_counts = {symbol: 0 for symbol in SYMBOLS}
    side_counts = {"LONG": 0, "SHORT": 0}

    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                total_lines += 1
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    invalid_json += 1
                    continue
                if not isinstance(row, dict) or row.get("schema") != COLLECTOR_SCHEMA:
                    invalid_schema += 1
                    continue

                symbol = str(row.get("symbol") or "").upper()
                if symbol not in SYMBOLS:
                    invalid_symbol += 1
                    continue

                source_side = row.get("source_side")
                liquidated_side = row.get("liquidated_position_side")
                if EXPECTED_SIDE_MAP.get(source_side) != liquidated_side:
                    invalid_side += 1
                    continue

                size = _finite_positive(row.get("size_raw"))
                price = _finite_positive(row.get("bankruptcy_price_raw"))
                if size is None or price is None:
                    invalid_numeric += 1
                    continue

                event_ts = row.get("event_ts_ms")
                source_ts = row.get("source_ts_ms")
                received_ts = row.get("received_ts_ms")
                if not all(isinstance(value, int) and value > 0 for value in (event_ts, source_ts, received_ts)):
                    invalid_timestamp += 1
                    continue

                event_times.append(event_ts)
                source_event_ms.append(float(source_ts - event_ts))
                receive_source_ms.append(float(received_ts - source_ts))
                if source_ts < event_ts or received_ts < source_ts:
                    timestamp_order_anomalies += 1

                key = str(row.get("event_key") or "")
                if not key:
                    invalid_schema += 1
                    continue
                if key in keys:
                    duplicate_keys += 1
                else:
                    keys.add(key)

                symbol_counts[symbol] += 1
                side_counts[liquidated_side] = side_counts.get(liquidated_side, 0) + 1
                quote_notional_usdt.append(size * price)

    span_hours = 0.0
    if len(event_times) >= 2:
        span_hours = (max(event_times) - min(event_times)) / 3_600_000.0

    def summary(values: list[float]) -> dict[str, Any] | None:
        if not values:
            return None
        ordered = sorted(values)
        def percentile(p: float) -> float:
            index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
            return ordered[index]
        return {
            "min": round(min(values), 4),
            "median": round(statistics.median(values), 4),
            "p95": round(percentile(0.95), 4),
            "max": round(max(values), 4),
        }

    return {
        "files": [str(path) for path in files],
        "total_lines": total_lines,
        "valid_unique_events": len(keys),
        "invalid_json": invalid_json,
        "invalid_schema": invalid_schema,
        "invalid_symbol": invalid_symbol,
        "invalid_side": invalid_side,
        "invalid_numeric": invalid_numeric,
        "invalid_timestamp": invalid_timestamp,
        "duplicate_event_keys_in_normalized_files": duplicate_keys,
        "timestamp_order_anomalies": timestamp_order_anomalies,
        "observation_span_hours": round(span_hours, 4),
        "symbol_counts": symbol_counts,
        "liquidated_side_counts": side_counts,
        "source_minus_event_ms": summary(source_event_ms),
        "received_minus_source_ms": summary(receive_source_ms),
        "quote_notional_usdt": summary(quote_notional_usdt),
        "quote_notional_note": "QA DERIVATION ONLY: size_raw * bankruptcy_price_raw; this is USDT quote notional, not USD.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.getenv("COLLECTOR_B_DATA_DIR", "C:/TradingCore_Collector_B/data"))
    parser.add_argument("--output", default="collector_b_audit_results")
    args = parser.parse_args()

    safety = assert_safe_startup()
    data_dir = Path(args.data_dir)
    status_file = data_dir / "status.json"
    if not status_file.exists():
        raise SystemExit(f"Collector B status not found: {status_file}")

    status = _read_json(status_file)
    hard_safety = {
        "collector_schema_ok": status.get("schema") == COLLECTOR_SCHEMA,
        "collector_id_ok": status.get("collector_id") == COLLECTOR_ID,
        "running": status.get("running") is True,
        "public_only": status.get("private_api_used") is False,
        "real_orders_disabled": status.get("real_orders_enabled") is False and status.get("real_order_sent") is False,
        "strategy_disabled": status.get("strategy_logic_enabled") is False,
        "outcomes_disabled": status.get("outcome_computation_enabled") is False,
        "collector_a_unchanged": status.get("collector_a_modified") is False,
    }

    instruments: dict[str, Any] = {}
    metadata_failures: list[str] = []
    for symbol in SYMBOLS:
        try:
            row = fetch_instrument(symbol)
            metadata = {
                "symbol": row.get("symbol"),
                "contractType": row.get("contractType"),
                "status": row.get("status"),
                "baseCoin": row.get("baseCoin"),
                "quoteCoin": row.get("quoteCoin"),
                "settleCoin": row.get("settleCoin"),
                "qtyStep": (row.get("lotSizeFilter") or {}).get("qtyStep") if isinstance(row.get("lotSizeFilter"), dict) else None,
                "tickSize": (row.get("priceFilter") or {}).get("tickSize") if isinstance(row.get("priceFilter"), dict) else None,
            }
            metadata["verified_linear_usdt"] = bool(
                metadata["symbol"] == symbol
                and metadata["contractType"] in ("LinearPerpetual", "LinearFutures")
                and metadata["status"] == "Trading"
                and metadata["quoteCoin"] in ("USDT", "USDC")
            )
            instruments[symbol] = metadata
            if not metadata["verified_linear_usdt"]:
                metadata_failures.append(symbol)
        except Exception as error:
            instruments[symbol] = {"error": f"{type(error).__name__}: {error}", "verified_linear_usdt": False}
            metadata_failures.append(symbol)

    try:
        probe = asyncio.run(subscription_probe())
    except Exception as error:
        probe = {"ack_received": False, "success": False, "error": f"{type(error).__name__}: {error}"}

    evidence = scan_evidence(data_dir)

    g2_failures: list[str] = []
    if not all(hard_safety.values()):
        g2_failures.append("HARD_SAFETY_OR_IDENTITY")
    if metadata_failures:
        g2_failures.append("INSTRUMENT_METADATA")
    if not probe.get("ack_received") or not probe.get("success"):
        g2_failures.append("PUBLIC_SUBSCRIPTION_ACK")
    if evidence["total_lines"] > 0:
        for key in (
            "invalid_json", "invalid_schema", "invalid_symbol", "invalid_side",
            "invalid_numeric", "invalid_timestamp",
        ):
            if evidence[key] != 0:
                g2_failures.append(f"EVIDENCE_{key.upper()}")
    else:
        g2_failures.append("EVENT_SAMPLE_PENDING")

    g2_passed = not g2_failures

    g3_failures: list[str] = []
    g3_pending: list[str] = []
    if evidence["valid_unique_events"] < G3_MIN_EVENTS:
        g3_pending.append(f"MIN_EVENTS_{G3_MIN_EVENTS}")
    if evidence["observation_span_hours"] < G3_MIN_SPAN_HOURS:
        g3_pending.append(f"MIN_SPAN_{G3_MIN_SPAN_HOURS:g}H")
    if int(status.get("reconnect_count") or 0) > 0:
        g3_failures.append("RECONNECT_GAP_ACCOUNTING_REQUIRED")
    if evidence["duplicate_event_keys_in_normalized_files"] > 0:
        g3_failures.append("NORMALIZED_DUPLICATES")
    if evidence["timestamp_order_anomalies"] > 0:
        g3_failures.append("TIMESTAMP_ORDER_ANOMALIES")
    if any(evidence[key] > 0 for key in (
        "invalid_json", "invalid_schema", "invalid_symbol", "invalid_side",
        "invalid_numeric", "invalid_timestamp",
    )):
        g3_failures.append("INVALID_NORMALIZED_RECORDS")

    if g3_failures:
        g3_state = "G3_REPAIR_REQUIRED"
    elif g3_pending:
        g3_state = "G3_PENDING_SAMPLE"
    else:
        g3_state = "G3_PRELIMINARY_PASS"

    report = {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "mode": "READ_ONLY_DATA_QUALITY_AUDIT",
        "safety": safety,
        "hard_safety": hard_safety,
        "subscription_probe": probe,
        "instrument_metadata": instruments,
        "evidence": evidence,
        "g2": {
            "passed": g2_passed,
            "state": "G2_PASS" if g2_passed else ("G2_PENDING_EVENT_SAMPLE" if g2_failures == ["EVENT_SAMPLE_PENDING"] else "G2_REPAIR_REQUIRED"),
            "failures_or_pending": g2_failures,
        },
        "g3": {
            "state": g3_state,
            "failures": g3_failures,
            "pending": g3_pending,
            "minimum_events": G3_MIN_EVENTS,
            "minimum_span_hours": G3_MIN_SPAN_HOURS,
            "note": "G3 preliminary pass concerns local capture quality only; it is not evidence of predictive edge or profitability.",
        },
        "research_gate": "NO_OUTCOME_RESEARCH_UNTIL_G2_PASS_AND_G3_PRELIMINARY_PASS",
        "collector_a_modified": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
    }

    out = Path(args.output)
    if not out.is_absolute():
        out = Path.cwd() / out
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"collector_b_g2_g3_{stamp}.json"
    latest_json = out / "LATEST_COLLECTOR_B_G2_G3.json"
    latest_txt = out / "LATEST_COLLECTOR_B_G2_G3.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    latest_json.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    lines = [
        "=" * 92,
        "TRADINGCORE COLLECTOR B — G2/G3 DATA QUALITY AUDIT",
        "=" * 92,
        f"Generated UTC: {report['generated_at_utc']}",
        f"Subscription ACK: {probe.get('ack_received')} success={probe.get('success')}",
        f"Events: {evidence['valid_unique_events']} span_hours={evidence['observation_span_hours']}",
        f"Reconnects: {int(status.get('reconnect_count') or 0)}",
        f"G2: {report['g2']['state']} issues={','.join(g2_failures) if g2_failures else 'NONE'}",
        f"G3: {g3_state} failures={','.join(g3_failures) if g3_failures else 'NONE'} pending={','.join(g3_pending) if g3_pending else 'NONE'}",
        "Outcome research: BLOCKED until G2 PASS + G3 PRELIMINARY PASS",
        "Collector A: UNCHANGED | Orders: DISABLED | LIVE: DISABLED",
    ]
    latest_txt.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    print("JSON:", json_path)
    print("LATEST:", latest_txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
