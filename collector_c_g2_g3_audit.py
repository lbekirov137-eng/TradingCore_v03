#!/usr/bin/env python3
"""Read-only G2/G3 audit for Collector C wide liquidation cohort."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_COLLECTOR_C_G2_G3_AUDIT_V1"
COLLECTOR_SCHEMA = "TRADINGCORE_COLLECTOR_C_BYBIT_WIDE_V1"
COLLECTOR_ID = "COLLECTOR_C_BYBIT_WIDE_PUBLIC_ALL_LIQUIDATION"
WS = "wss://stream.bybit.com/v5/public/linear"
REST = "https://api.bybit.com"
G3_MIN_EVENTS = 100
G3_MIN_SPAN_HOURS = 6.0


def http_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urlencode(params or {})
    request = Request(f"{REST}{path}" + (f"?{query}" if query else ""), headers={"Accept": "application/json", "User-Agent": "TradingCore-CollectorC-Audit/1.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit public REST failed {path}: {payload}")
    return payload


def clock_probe() -> dict[str, Any]:
    before = time.time() * 1000.0
    payload = http_json("/v5/market/time")
    after = time.time() * 1000.0
    result = payload.get("result") or {}
    try:
        server_ms = int(str(result.get("timeNano"))) / 1_000_000.0
    except Exception:
        try:
            server_ms = float(result.get("timeSecond")) * 1000.0
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    midpoint = (before + after) / 2.0
    return {"ok": True, "local_minus_server_ms": midpoint - server_ms, "round_trip_ms": after - before}


def canonical_lock_fingerprint(lock: dict[str, Any]) -> str:
    body = dict(lock); body.pop("fingerprint", None)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def subscription_probe(topics: list[str]) -> dict[str, Any]:
    req = f"collector-c-audit-{int(time.time()*1000)}"
    async with connect(WS, open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2_000_000) as websocket:
        await websocket.send(json.dumps({"req_id": req, "op": "subscribe", "args": topics}))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.1, deadline-time.monotonic()))
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("op") == "subscribe":
                return {"ack_received": True, "success": payload.get("success") is True, "raw": payload}
    return {"ack_received": False, "success": False}


def scan_epoch(epoch: Path, symbols: set[str], clock_offset_ms: float) -> dict[str, Any]:
    files = sorted((epoch / "normalized").glob("*.jsonl")) if (epoch / "normalized").exists() else []
    total = invalid = duplicates = hard_ts = 0
    keys: set[str] = set(); times: list[int] = []; source_event: list[float] = []; receive_source: list[float] = []
    symbol_counts = {s: 0 for s in symbols}; side_counts = {"LONG": 0, "SHORT": 0}
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip(): continue
                total += 1
                try: row = json.loads(line)
                except json.JSONDecodeError: invalid += 1; continue
                if not isinstance(row, dict) or row.get("schema") != COLLECTOR_SCHEMA: invalid += 1; continue
                symbol = str(row.get("symbol") or "").upper(); side = str(row.get("liquidated_position_side") or "").upper()
                if symbol not in symbols or side not in ("LONG","SHORT"): invalid += 1; continue
                try:
                    size=float(row.get("size_raw")); price=float(row.get("bankruptcy_price_raw")); event=int(row.get("event_ts_ms")); source=int(row.get("source_ts_ms")); received=int(row.get("received_ts_ms"))
                except (TypeError,ValueError): invalid += 1; continue
                if not (math.isfinite(size) and size>0 and math.isfinite(price) and price>0 and event>0 and source>0 and received>0): invalid += 1; continue
                key=str(row.get("event_key") or "")
                if not key: invalid += 1; continue
                if key in keys: duplicates += 1
                else: keys.add(key)
                times.append(event); symbol_counts[symbol]+=1; side_counts[side]+=1
                se=float(source-event); rs=float((received-clock_offset_ms)-source)
                source_event.append(se); receive_source.append(rs)
                # Hard only when exchange generation/update relationship is extreme,
                # or clock-corrected receive latency is impossible/excessive.
                if abs(se) > 10_000 or rs < -5_000 or rs > 60_000: hard_ts += 1
    span=0.0 if len(times)<2 else (max(times)-min(times))/3_600_000.0
    def summary(values:list[float])->dict[str,float]|None:
        if not values:return None
        ordered=sorted(values); idx=min(len(ordered)-1,max(0,int(round((len(ordered)-1)*0.95))))
        return {"min":round(min(values),3),"median":round(statistics.median(values),3),"p95":round(ordered[idx],3),"max":round(max(values),3)}
    return {"files":[str(p) for p in files],"total_lines":total,"valid_unique_events":len(keys),"invalid_records":invalid,"duplicate_event_keys":duplicates,"timestamp_hard_anomalies":hard_ts,"observation_span_hours":round(span,4),"symbol_counts":symbol_counts,"side_counts":side_counts,"source_minus_event_ms":summary(source_event),"clock_corrected_received_minus_source_ms":summary(receive_source)}


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--data-dir",default="C:/TradingCore_Collector_C/data"); parser.add_argument("--output",default="collector_c_audit_results"); args=parser.parse_args()
    safety=assert_safe_startup(); data=Path(args.data_dir); status_path=data/"status.json"; lock_path=data/"UNIVERSE_LOCK.json"; current_path=data/"CURRENT_EPOCH.json"
    if not status_path.exists() or not lock_path.exists() or not current_path.exists(): raise SystemExit("Collector C status/universe/current epoch missing")
    status=json.loads(status_path.read_text(encoding="utf-8-sig")); lock=json.loads(lock_path.read_text(encoding="utf-8-sig")); current=json.loads(current_path.read_text(encoding="utf-8-sig"))
    symbols=[str(s).upper() for s in lock.get("symbols") or []]; topics=[f"allLiquidation.{s}" for s in symbols]
    clock=clock_probe(); offset=float(clock.get("local_minus_server_ms") or 0.0)
    probe=asyncio.run(subscription_probe(topics))
    epoch_id=str(current.get("epoch_id") or ""); epoch=data/"epochs"/epoch_id; evidence=scan_epoch(epoch,set(symbols),offset)
    lock_ok=(lock.get("schema")=="TRADINGCORE_COLLECTOR_C_UNIVERSE_LOCK_V1" and lock.get("fingerprint")==canonical_lock_fingerprint(lock) and len(symbols)>=10)
    hard={"identity":status.get("schema")==COLLECTOR_SCHEMA and status.get("collector_id")==COLLECTOR_ID,"running":status.get("running") is True,"connected":status.get("connection_state")=="CONNECTED","public_only":status.get("private_api_used") is False,"orders_disabled":status.get("real_orders_enabled") is False and status.get("real_order_sent") is False,"strategy_disabled":status.get("strategy_logic_enabled") is False,"outcomes_disabled":status.get("outcome_computation_enabled") is False,"universe_lock":lock_ok,"epoch_matches":status.get("epoch_id")==epoch_id}
    g2_fail=[]
    if not all(hard.values()): g2_fail.append("HARD_SAFETY_IDENTITY_OR_EPOCH")
    if not (probe.get("ack_received") and probe.get("success")): g2_fail.append("PUBLIC_SUBSCRIPTION_ACK")
    if not clock.get("ok"): g2_fail.append("CLOCK_PROBE")
    if evidence["total_lines"]>0 and evidence["invalid_records"]>0:g2_fail.append("INVALID_EVIDENCE")
    elif evidence["total_lines"]==0:g2_fail.append("EVENT_SAMPLE_PENDING")
    g2_state="G2_PASS" if not g2_fail else ("G2_PENDING_EVENT_SAMPLE" if g2_fail==["EVENT_SAMPLE_PENDING"] else "G2_REPAIR_REQUIRED")
    g3_fail=[]; g3_pending=[]
    if evidence["invalid_records"]>0:g3_fail.append("INVALID_NORMALIZED_RECORDS")
    if evidence["duplicate_event_keys"]>0:g3_fail.append("NORMALIZED_DUPLICATES")
    if evidence["timestamp_hard_anomalies"]>0:g3_fail.append("TIMESTAMP_HARD_ANOMALIES")
    if evidence["valid_unique_events"]<G3_MIN_EVENTS:g3_pending.append(f"MIN_EVENTS_{G3_MIN_EVENTS}")
    if evidence["observation_span_hours"]<G3_MIN_SPAN_HOURS:g3_pending.append(f"MIN_SPAN_{G3_MIN_SPAN_HOURS:g}H")
    g3_state="G3_REPAIR_REQUIRED" if g3_fail else ("G3_PENDING_SAMPLE" if g3_pending else "G3_PRELIMINARY_PASS")
    report={"schema":SCHEMA,"generated_at_utc":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"mode":"READ_ONLY_DATA_QUALITY_AUDIT","safety":safety,"hard_safety":hard,"clock_probe":clock,"subscription_probe":probe,"universe":{"symbols":symbols,"fingerprint":lock.get("fingerprint"),"selection_rule":lock.get("selection_rule")},"current_epoch":epoch_id,"evidence":evidence,"g2":{"state":g2_state,"issues":g2_fail},"g3":{"state":g3_state,"failures":g3_fail,"pending":g3_pending,"minimum_events":G3_MIN_EVENTS,"minimum_span_hours":G3_MIN_SPAN_HOURS},"collector_a_modified":False,"collector_b_modified":False,"real_orders_enabled":False}
    out=Path(args.output); out=out if out.is_absolute() else Path.cwd()/out; out.mkdir(parents=True,exist_ok=True); stamp=time.strftime('%Y%m%d_%H%M%S',time.gmtime()); payload=json.dumps(report,indent=2,ensure_ascii=False,default=str); (out/f"collector_c_g2_g3_{stamp}.json").write_text(payload,encoding="utf-8"); (out/"LATEST_COLLECTOR_C_G2_G3.json").write_text(payload,encoding="utf-8")
    print("="*92); print("TRADINGCORE COLLECTOR C — WIDE G2/G3 AUDIT"); print(f"Universe: {len(symbols)} symbols fingerprint={lock.get('fingerprint')}"); print(f"Epoch: {epoch_id}"); print(f"Events: {evidence['valid_unique_events']} span_hours={evidence['observation_span_hours']}"); print(f"G2: {g2_state} issues={','.join(g2_fail) if g2_fail else 'NONE'}"); print(f"G3: {g3_state} failures={','.join(g3_fail) if g3_fail else 'NONE'} pending={','.join(g3_pending) if g3_pending else 'NONE'}"); print("Collector A/B: UNCHANGED | Orders/LIVE: DISABLED"); print("="*92)
    return 0

if __name__=="__main__": raise SystemExit(main())
