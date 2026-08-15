#!/usr/bin/env python3
"""Collector C: wide, isolated Bybit all-liquidation capture.

RESEARCH DATA COLLECTION ONLY. No private API, account access, strategy logic,
outcome computation, or order path.

At first start the collector freezes a universe of the highest-turnover Bybit
USDT linear perpetuals that also have an active USDT spot market. The universe
is selected from PUBLIC REST metadata/tickers before any liquidation outcomes
are inspected and is persisted forever in UNIVERSE_LOCK.json.

Every process start and every WebSocket reconnect begins a new evidence epoch.
This makes possible transport gaps explicit rather than pretending a continuous
sample. Old epochs are never rewritten or deleted.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect

from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_COLLECTOR_C_BYBIT_WIDE_V1"
COLLECTOR_ID = "COLLECTOR_C_BYBIT_WIDE_PUBLIC_ALL_LIQUIDATION"
VENUE = "BYBIT"
COHORT = "BYBIT_WIDE_ALL_LIQUIDATION_LINEAR_PUBLIC_V1"
PUBLIC_WS_URL = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_REST_BASE = "https://api.bybit.com"
DEFAULT_DATA_DIR = "C:/TradingCore_Collector_C/data"
UNIVERSE_SIZE = 20
HEARTBEAT_SECONDS = 20
STATUS_SECONDS = 15
RECENT_KEYS = 20000

_stop = asyncio.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


def root() -> Path:
    return Path(os.getenv("COLLECTOR_C_DATA_DIR", DEFAULT_DATA_DIR))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        handle.flush()


def http_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{PUBLIC_REST_BASE}{path}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "TradingCore-CollectorC/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit public REST failed {path}: {payload}")
    return payload


def all_instruments(category: str) -> list[dict[str, Any]]:
    if category == "spot":
        payload = http_json("/v5/market/instruments-info", {"category": "spot"})
        rows = ((payload.get("result") or {}).get("list") or [])
        return [row for row in rows if isinstance(row, dict)]

    cursor = ""
    result: list[dict[str, Any]] = []
    for _ in range(20):
        params: dict[str, Any] = {"category": category, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = http_json("/v5/market/instruments-info", params)
        body = payload.get("result") or {}
        rows = body.get("list") if isinstance(body, dict) else None
        if isinstance(rows, list):
            result.extend(row for row in rows if isinstance(row, dict))
        cursor = str(body.get("nextPageCursor") or "") if isinstance(body, dict) else ""
        if not cursor:
            break
    return result


def build_universe_lock(path: Path) -> dict[str, Any]:
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("schema") != "TRADINGCORE_COLLECTOR_C_UNIVERSE_LOCK_V1":
            raise RuntimeError("Collector C universe lock schema mismatch")
        return payload

    linear = all_instruments("linear")
    spot = all_instruments("spot")
    linear_ok = {
        str(row.get("symbol") or "").upper()
        for row in linear
        if row.get("status") == "Trading"
        and row.get("contractType") == "LinearPerpetual"
        and row.get("quoteCoin") == "USDT"
        and row.get("settleCoin") == "USDT"
    }
    spot_ok = {
        str(row.get("symbol") or "").upper()
        for row in spot
        if row.get("status") == "Trading" and row.get("quoteCoin") == "USDT"
    }
    eligible = linear_ok & spot_ok

    tickers = http_json("/v5/market/tickers", {"category": "linear"})
    rows = ((tickers.get("result") or {}).get("list") or [])
    ranked: list[tuple[float, str]] = []
    snapshot: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in eligible:
            continue
        try:
            turnover = float(row.get("turnover24h"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(turnover) or turnover <= 0:
            continue
        ranked.append((turnover, symbol))
        snapshot[symbol] = turnover
    ranked.sort(reverse=True)
    symbols = [symbol for _, symbol in ranked[:UNIVERSE_SIZE]]
    if len(symbols) < 10:
        raise RuntimeError(f"Too few eligible wide-universe symbols: {len(symbols)}")

    body = {
        "schema": "TRADINGCORE_COLLECTOR_C_UNIVERSE_LOCK_V1",
        "created_at_utc": utc_now(),
        "selection_rule": (
            "Top 20 Bybit Trading LinearPerpetual USDT-settled contracts by public turnover24h "
            "that also have an active USDT spot market; frozen before liquidation outcome research."
        ),
        "universe_size": len(symbols),
        "symbols": symbols,
        "turnover24h_usdt_at_lock": {symbol: snapshot[symbol] for symbol in symbols},
        "private_api_used": False,
        "real_orders_enabled": False,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    atomic_json(path, body)
    return body


def event_key(item: dict[str, Any]) -> str:
    material = "|".join(str(item.get(name, "")) for name in ("T", "s", "S", "v", "p"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def new_epoch_id() -> str:
    return datetime.now(timezone.utc).strftime("EPOCH_%Y%m%d_%H%M%S_%f")


def epoch_dir(epoch_id: str) -> Path:
    return root() / "epochs" / epoch_id


def start_epoch(state: dict[str, Any], reason: str) -> None:
    epoch_id = new_epoch_id()
    state["epoch_id"] = epoch_id
    state["epoch_started_at_utc"] = utc_now()
    state["epoch_started_ms"] = now_ms()
    state["epoch_events_written"] = 0
    state["epoch_symbol_counts"] = {symbol: 0 for symbol in state["symbols"]}
    state["epoch_reason"] = reason
    directory = epoch_dir(epoch_id)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(
        directory / "epoch_manifest.json",
        {
            "schema": SCHEMA,
            "epoch_id": epoch_id,
            "started_at_utc": state["epoch_started_at_utc"],
            "start_reason": reason,
            "symbols": state["symbols"],
            "universe_fingerprint": state["universe_fingerprint"],
            "closed": False,
            "private_api_used": False,
            "real_orders_enabled": False,
        },
    )
    atomic_json(root() / "CURRENT_EPOCH.json", {"epoch_id": epoch_id, "started_at_utc": state["epoch_started_at_utc"]})


def close_epoch(state: dict[str, Any], reason: str) -> None:
    epoch_id = state.get("epoch_id")
    if not epoch_id:
        return
    path = epoch_dir(str(epoch_id)) / "epoch_manifest.json"
    manifest: dict[str, Any] = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            manifest = {}
    manifest.update(
        closed=True,
        closed_at_utc=utc_now(),
        close_reason=reason,
        events_written=int(state.get("epoch_events_written") or 0),
        symbol_counts=state.get("epoch_symbol_counts") or {},
        private_api_used=False,
        real_orders_enabled=False,
    )
    atomic_json(path, manifest)
    append_jsonl(
        root() / "EPOCH_LEDGER.jsonl",
        {
            "schema": "TRADINGCORE_COLLECTOR_C_EPOCH_LEDGER_V1",
            "epoch_id": epoch_id,
            "started_at_utc": state.get("epoch_started_at_utc"),
            "closed_at_utc": manifest["closed_at_utc"],
            "reason": reason,
            "events_written": manifest["events_written"],
            "evidence_preserved": True,
            "real_orders_enabled": False,
        },
    )


def normalized(item: dict[str, Any], source_ts: Any, received_ms: int, state: dict[str, Any]) -> dict[str, Any]:
    symbol = str(item.get("s") or "").upper()
    source_side = str(item.get("S") or "")
    side = "LONG" if source_side == "Buy" else ("SHORT" if source_side == "Sell" else None)
    return {
        "schema": SCHEMA,
        "collector_id": COLLECTOR_ID,
        "venue": VENUE,
        "cohort": COHORT,
        "epoch_id": state["epoch_id"],
        "universe_fingerprint": state["universe_fingerprint"],
        "source": "BYBIT_PUBLIC_LINEAR_ALL_LIQUIDATION",
        "topic": f"allLiquidation.{symbol}",
        "event_key": event_key(item),
        "source_ts_ms": int(source_ts) if isinstance(source_ts, (int, float)) else None,
        "event_ts_ms": int(item.get("T")) if isinstance(item.get("T"), (int, float)) else None,
        "received_ts_ms": received_ms,
        "received_at_utc": datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc).isoformat(),
        "symbol": symbol,
        "source_side": source_side,
        "liquidated_position_side": side,
        "size_raw": str(item.get("v")) if item.get("v") is not None else None,
        "bankruptcy_price_raw": str(item.get("p")) if item.get("p") is not None else None,
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
    }


def valid_item(item: Any, symbols: set[str]) -> bool:
    if not isinstance(item, dict):
        return False
    if str(item.get("s") or "").upper() not in symbols or item.get("S") not in ("Buy", "Sell"):
        return False
    try:
        return float(item.get("v")) > 0 and float(item.get("p")) > 0 and int(item.get("T")) > 0
    except (TypeError, ValueError):
        return False


def write_status(state: dict[str, Any], connection_state: str, detail: str | None = None) -> None:
    atomic_json(
        root() / "status.json",
        {
            "schema": SCHEMA,
            "collector_id": COLLECTOR_ID,
            "mode": "RESEARCH_DATA_COLLECTION_ONLY",
            "running": not _stop.is_set(),
            "connection_state": connection_state,
            "detail": detail,
            "venue": VENUE,
            "cohort": COHORT,
            "public_ws_url": PUBLIC_WS_URL,
            "symbols": state["symbols"],
            "topics": [f"allLiquidation.{s}" for s in state["symbols"]],
            "universe_fingerprint": state["universe_fingerprint"],
            "epoch_id": state.get("epoch_id"),
            "epoch_started_at_utc": state.get("epoch_started_at_utc"),
            "epoch_events_written": int(state.get("epoch_events_written") or 0),
            "events_written_lifetime": int(state.get("events_written_lifetime") or 0),
            "epoch_symbol_counts": state.get("epoch_symbol_counts") or {},
            "reconnect_count_lifetime": int(state.get("reconnect_count_lifetime") or 0),
            "last_error": state.get("last_error"),
            "private_api_used": False,
            "real_orders_enabled": False,
            "real_order_sent": False,
            "strategy_logic_enabled": False,
            "outcome_computation_enabled": False,
            "collector_a_modified": False,
            "collector_b_modified": False,
            "updated_at_utc": utc_now(),
        },
    )


async def heartbeat(ws: Any) -> None:
    while not _stop.is_set():
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await ws.send(json.dumps({"op": "ping"}))
        except Exception:
            return


async def status_loop(state: dict[str, Any]) -> None:
    while not _stop.is_set():
        write_status(state, str(state.get("connection_state") or "UNKNOWN"))
        await asyncio.sleep(STATUS_SECONDS)


async def connection(state: dict[str, Any], recent: deque[str], recent_set: set[str]) -> None:
    topics = [f"allLiquidation.{symbol}" for symbol in state["symbols"]]
    async with connect(PUBLIC_WS_URL, open_timeout=15, ping_interval=20, ping_timeout=20, close_timeout=10, max_size=4_000_000, max_queue=256) as ws:
        state["connection_state"] = "CONNECTED"
        state["last_error"] = None
        write_status(state, "CONNECTED", "subscribing")
        await ws.send(json.dumps({"op": "subscribe", "args": topics}))
        hb = asyncio.create_task(heartbeat(ws))
        try:
            async for raw in ws:
                received_ms = now_ms()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or not str(payload.get("topic") or "").startswith("allLiquidation."):
                    continue
                data = payload.get("data")
                if isinstance(data, dict):
                    data = [data]
                if not isinstance(data, list):
                    continue
                source_ts = payload.get("ts")
                symbols = set(state["symbols"])
                for item in data:
                    if not valid_item(item, symbols):
                        continue
                    key = event_key(item)
                    if key in recent_set:
                        continue
                    row = normalized(item, source_ts, received_ms, state)
                    day = datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
                    base = epoch_dir(state["epoch_id"])
                    append_jsonl(base / "raw" / f"{day}.jsonl", {"schema": SCHEMA, "epoch_id": state["epoch_id"], "received_ts_ms": received_ms, "raw_payload": payload, "real_order_sent": False})
                    append_jsonl(base / "normalized" / f"{day}.jsonl", row)
                    if len(recent) >= recent.maxlen and recent:
                        recent_set.discard(recent[0])
                    recent.append(key)
                    recent_set.add(key)
                    state["epoch_events_written"] = int(state.get("epoch_events_written") or 0) + 1
                    state["events_written_lifetime"] = int(state.get("events_written_lifetime") or 0) + 1
                    counts = state.setdefault("epoch_symbol_counts", {s: 0 for s in state["symbols"]})
                    symbol = row["symbol"]
                    counts[symbol] = int(counts.get(symbol) or 0) + 1
                    state["last_event_at_utc"] = row["received_at_utc"]
                write_status(state, "CONNECTED")
        finally:
            hb.cancel()


async def run_forever() -> int:
    safety = assert_safe_startup()
    data = root()
    data.mkdir(parents=True, exist_ok=True)
    lock = build_universe_lock(data / "UNIVERSE_LOCK.json")
    state: dict[str, Any] = {
        "symbols": list(lock["symbols"]),
        "universe_fingerprint": lock["fingerprint"],
        "events_written_lifetime": 0,
        "reconnect_count_lifetime": 0,
        "last_error": None,
    }
    recent: deque[str] = deque(maxlen=RECENT_KEYS)
    recent_set: set[str] = set()
    start_epoch(state, "PROCESS_START")
    status_task = asyncio.create_task(status_loop(state))
    backoff = 1.0
    print("=" * 88)
    print("TRADINGCORE COLLECTOR C — WIDE BYBIT PUBLIC ALL LIQUIDATIONS")
    print("Safety:", safety)
    print("Universe:", ",".join(state["symbols"]))
    print("Universe fingerprint:", state["universe_fingerprint"])
    print("PRIVATE API: NO | ORDERS: IMPOSSIBLE | Collector A/B: UNCHANGED")
    print("=" * 88, flush=True)
    try:
        while not _stop.is_set():
            try:
                await connection(state, recent, recent_set)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                state["reconnect_count_lifetime"] = int(state.get("reconnect_count_lifetime") or 0) + 1
                state["last_error"] = f"{type(error).__name__}: {error}"
                state["connection_state"] = "RECONNECTING"
                write_status(state, "RECONNECTING", state["last_error"])
                close_epoch(state, "WEBSOCKET_RECONNECT_GAP")
                start_epoch(state, "AFTER_RECONNECT")
                recent.clear(); recent_set.clear()
                print(f"[COLLECTOR C] new clean epoch after reconnect: {state['last_error']}", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
    finally:
        _stop.set()
        status_task.cancel()
        close_epoch(state, "PROCESS_STOP")
        write_status(state, "STOPPED")
    return 0


def request_stop(*_: Any) -> None:
    _stop.set()


def main() -> int:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass
    return asyncio.run(run_forever())


if __name__ == "__main__":
    raise SystemExit(main())
