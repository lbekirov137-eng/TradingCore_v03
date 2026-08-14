#!/usr/bin/env python3
"""
TradingCore Collector B — Bybit public full-liquidation feed.

RESEARCH / DATA COLLECTION ONLY.

Hard isolation properties:
- public WebSocket only: wss://stream.bybit.com/v5/public/linear
- topics: allLiquidation.BTCUSDT / ETHUSDT / SOLUSDT
- no private/auth stream
- no exchange order client
- no account balances
- no strategy logic
- no outcome computation
- no threshold / qualifying-episode decisions
- no modification of Collector A
- no LIVE or PAPER order path

Collector B writes append-only raw and normalized JSONL plus atomic health and
checkpoint metadata. The normalized layer intentionally does NOT derive USD
notional or trading outcomes. Contract-size/notional semantics must pass the
separate data-normalization gate before any outcome research.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_COLLECTOR_B_BYBIT_V1"
COLLECTOR_ID = "COLLECTOR_B_BYBIT_PUBLIC_ALL_LIQUIDATION"
VENUE = "BYBIT"
COHORT = "BYBIT_ALL_LIQUIDATION_LINEAR_PUBLIC_V1"
SOURCE_COMPLETENESS_CLASS = "BYBIT_DOCUMENTED_ALL_LIQUIDATIONS"
PUBLIC_WS_URL = "wss://stream.bybit.com/v5/public/linear"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TOPICS = tuple(f"allLiquidation.{symbol}" for symbol in SYMBOLS)
REAL_ORDERS_ENABLED = False
PRIVATE_API_USED = False
OUTCOME_COMPUTATION_ENABLED = False
QUALIFYING_EPISODE_LOGIC_ENABLED = False

DEFAULT_DATA_DIR = "C:/TradingCore_Collector_B/data"
DEFAULT_RECENT_DEDUPE_KEYS = 5000
HEARTBEAT_SECONDS = 20
STATUS_SECONDS = 15

_stop = asyncio.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


def data_root() -> Path:
    return Path(os.getenv("COLLECTOR_B_DATA_DIR", DEFAULT_DATA_DIR))


def _date_key(ms: int | None = None) -> str:
    if ms is None:
        moment = datetime.now(timezone.utc)
    else:
        moment = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
        handle.write("\n")
        handle.flush()


def _event_key(item: dict[str, Any]) -> str:
    material = "|".join(
        str(item.get(name, ""))
        for name in ("T", "s", "S", "v", "p")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _load_checkpoint() -> dict[str, Any]:
    path = data_root() / "checkpoint.json"
    if not path.exists():
        return {
            "schema": SCHEMA,
            "collector_id": COLLECTOR_ID,
            "recent_event_keys": [],
            "events_written": 0,
            "duplicates_skipped": 0,
            "reconnect_count": 0,
            "symbol_counts": {symbol: 0 for symbol in SYMBOLS},
            "last_event_ts_ms": {symbol: None for symbol in SYMBOLS},
            "real_order_sent": False,
        }
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema") != SCHEMA or payload.get("collector_id") != COLLECTOR_ID:
        raise RuntimeError("Collector B checkpoint schema/identity mismatch")
    if payload.get("real_order_sent") is not False:
        raise RuntimeError("Unsafe Collector B checkpoint: real_order_sent must be false")
    return payload


def _save_checkpoint(state: dict[str, Any], recent: deque[str]) -> None:
    state["recent_event_keys"] = list(recent)
    state["updated_at_utc"] = utc_now()
    state["real_order_sent"] = False
    _atomic_json(data_root() / "checkpoint.json", state)


def _write_status(state: dict[str, Any], *, connection_state: str, detail: str | None = None) -> None:
    payload = {
        "schema": SCHEMA,
        "collector_id": COLLECTOR_ID,
        "mode": "RESEARCH_DATA_COLLECTION_ONLY",
        "running": not _stop.is_set(),
        "connection_state": connection_state,
        "detail": detail,
        "public_ws_url": PUBLIC_WS_URL,
        "venue": VENUE,
        "cohort": COHORT,
        "source_completeness_class": SOURCE_COMPLETENESS_CLASS,
        "symbols": list(SYMBOLS),
        "topics": list(TOPICS),
        "events_written": int(state.get("events_written") or 0),
        "duplicates_skipped": int(state.get("duplicates_skipped") or 0),
        "reconnect_count": int(state.get("reconnect_count") or 0),
        "symbol_counts": dict(state.get("symbol_counts") or {}),
        "last_event_ts_ms": dict(state.get("last_event_ts_ms") or {}),
        "last_error": state.get("last_error"),
        "private_api_used": PRIVATE_API_USED,
        "real_orders_enabled": REAL_ORDERS_ENABLED,
        "real_order_sent": False,
        "strategy_logic_enabled": False,
        "outcome_computation_enabled": OUTCOME_COMPUTATION_ENABLED,
        "qualifying_episode_logic_enabled": QUALIFYING_EPISODE_LOGIC_ENABLED,
        "collector_a_modified": False,
        "updated_at_utc": utc_now(),
    }
    _atomic_json(data_root() / "status.json", payload)


def _write_daily_manifest(state: dict[str, Any]) -> None:
    day = _date_key()
    payload = {
        "schema": SCHEMA,
        "collector_id": COLLECTOR_ID,
        "date_utc": day,
        "venue": VENUE,
        "cohort": COHORT,
        "symbols": list(SYMBOLS),
        "source_completeness_class": SOURCE_COMPLETENESS_CLASS,
        "raw_path": f"raw/bybit/{day}.jsonl",
        "normalized_path": f"normalized/bybit/{day}.jsonl",
        "events_written_lifetime": int(state.get("events_written") or 0),
        "symbol_counts_lifetime": dict(state.get("symbol_counts") or {}),
        "notional_semantics": "NOT_DERIVED_UNTIL_G2_NORMALIZATION_GATE",
        "outcomes": "NOT_COMPUTED",
        "orders": "IMPOSSIBLE_FROM_COLLECTOR",
        "updated_at_utc": utc_now(),
    }
    _atomic_json(data_root() / "manifests" / f"{day}.json", payload)


def _normalize(item: dict[str, Any], *, source_ts_ms: int | None, received_ms: int) -> dict[str, Any]:
    symbol = str(item.get("s") or "").upper()
    source_side = str(item.get("S") or "")
    liquidated_position_side = None
    if source_side == "Buy":
        liquidated_position_side = "LONG"
    elif source_side == "Sell":
        liquidated_position_side = "SHORT"

    return {
        "schema": SCHEMA,
        "collector_id": COLLECTOR_ID,
        "venue": VENUE,
        "cohort": COHORT,
        "source": "BYBIT_PUBLIC_LINEAR_ALL_LIQUIDATION",
        "source_completeness_class": SOURCE_COMPLETENESS_CLASS,
        "topic": f"allLiquidation.{symbol}" if symbol else None,
        "event_key": _event_key(item),
        "source_ts_ms": int(source_ts_ms) if isinstance(source_ts_ms, (int, float)) else None,
        "event_ts_ms": int(item.get("T")) if isinstance(item.get("T"), (int, float)) else None,
        "received_ts_ms": received_ms,
        "received_at_utc": datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc).isoformat(),
        "symbol": symbol,
        "source_side": source_side,
        "liquidated_position_side": liquidated_position_side,
        "size_raw": str(item.get("v")) if item.get("v") is not None else None,
        "bankruptcy_price_raw": str(item.get("p")) if item.get("p") is not None else None,
        "usd_notional": None,
        "usd_notional_status": "NOT_DERIVED_UNTIL_CONTRACT_METADATA_GATE",
        "threshold_cohort": None,
        "qualifying_episode": None,
        "outcome_fields": None,
        "private_api_used": False,
        "real_orders_enabled": False,
        "real_order_sent": False,
    }


def _valid_liquidation_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    symbol = str(item.get("s") or "").upper()
    if symbol not in SYMBOLS:
        return False
    if item.get("S") not in ("Buy", "Sell"):
        return False
    try:
        float(item.get("v"))
        float(item.get("p"))
        int(item.get("T"))
    except (TypeError, ValueError):
        return False
    return True


async def _heartbeat(websocket: Any) -> None:
    while not _stop.is_set():
        await asyncio.sleep(HEARTBEAT_SECONDS)
        if _stop.is_set():
            return
        try:
            await websocket.send(json.dumps({"op": "ping"}))
        except Exception:
            return


async def _status_loop(state: dict[str, Any]) -> None:
    while not _stop.is_set():
        _write_status(state, connection_state=str(state.get("connection_state") or "UNKNOWN"))
        _write_daily_manifest(state)
        await asyncio.sleep(STATUS_SECONDS)


async def _run_connection(state: dict[str, Any], recent: deque[str], recent_set: set[str]) -> None:
    async with connect(
        PUBLIC_WS_URL,
        open_timeout=15,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=2_000_000,
        max_queue=64,
    ) as websocket:
        state["connection_state"] = "CONNECTED"
        state["last_error"] = None
        _write_status(state, connection_state="CONNECTED", detail="subscribing")

        await websocket.send(json.dumps({"op": "subscribe", "args": list(TOPICS)}))
        heartbeat_task = asyncio.create_task(_heartbeat(websocket))

        try:
            async for raw_message in websocket:
                received_ms = now_ms()
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue

                if not isinstance(payload, dict):
                    continue

                topic = str(payload.get("topic") or "")
                if not topic.startswith("allLiquidation."):
                    continue

                data = payload.get("data")
                if isinstance(data, dict):
                    data = [data]
                if not isinstance(data, list):
                    continue

                raw_record = {
                    "schema": SCHEMA,
                    "collector_id": COLLECTOR_ID,
                    "received_ts_ms": received_ms,
                    "received_at_utc": datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc).isoformat(),
                    "venue": VENUE,
                    "cohort": COHORT,
                    "raw_payload": payload,
                    "real_order_sent": False,
                }
                day = _date_key(received_ms)
                _append_jsonl(data_root() / "raw" / "bybit" / f"{day}.jsonl", raw_record)

                source_ts = payload.get("ts")
                for item in data:
                    if not _valid_liquidation_item(item):
                        continue

                    key = _event_key(item)
                    if key in recent_set:
                        state["duplicates_skipped"] = int(state.get("duplicates_skipped") or 0) + 1
                        continue

                    normalized = _normalize(item, source_ts_ms=source_ts, received_ms=received_ms)
                    _append_jsonl(
                        data_root() / "normalized" / "bybit" / f"{day}.jsonl",
                        normalized,
                    )

                    if len(recent) >= recent.maxlen and recent:
                        expired = recent[0]
                        recent_set.discard(expired)
                    recent.append(key)
                    recent_set.add(key)

                    symbol = normalized["symbol"]
                    counts = state.setdefault("symbol_counts", {s: 0 for s in SYMBOLS})
                    counts[symbol] = int(counts.get(symbol) or 0) + 1
                    state.setdefault("last_event_ts_ms", {s: None for s in SYMBOLS})[symbol] = normalized["event_ts_ms"]
                    state["events_written"] = int(state.get("events_written") or 0) + 1
                    state["last_event_at_utc"] = normalized["received_at_utc"]
                    state["last_error"] = None

                _save_checkpoint(state, recent)
                _write_status(state, connection_state="CONNECTED")

        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


async def run_forever() -> int:
    safety = assert_safe_startup()
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)

    state = _load_checkpoint()
    recent = deque(
        [str(value) for value in state.get("recent_event_keys", [])],
        maxlen=DEFAULT_RECENT_DEDUPE_KEYS,
    )
    recent_set = set(recent)

    print("=" * 84)
    print("TRADINGCORE COLLECTOR B — BYBIT PUBLIC ALL LIQUIDATIONS")
    print("=" * 84)
    print("Safety:", safety)
    print("URL:", PUBLIC_WS_URL)
    print("Topics:", ", ".join(TOPICS))
    print("Data:", root)
    print("PRIVATE API: NOT USED")
    print("STRATEGY / OUTCOMES: DISABLED")
    print("REAL ORDERS: IMPOSSIBLE FROM THIS COLLECTOR")
    print("COLLECTOR A: UNCHANGED")
    print("=" * 84, flush=True)

    status_task = asyncio.create_task(_status_loop(state))
    backoff = 1.0

    try:
        while not _stop.is_set():
            try:
                await _run_connection(state, recent, recent_set)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                state["connection_state"] = "RECONNECTING"
                state["reconnect_count"] = int(state.get("reconnect_count") or 0) + 1
                state["last_error"] = f"{type(error).__name__}: {error}"
                _save_checkpoint(state, recent)
                _write_status(state, connection_state="RECONNECTING", detail=state["last_error"])
                print(f"[COLLECTOR B] reconnect after error: {state['last_error']}", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
    finally:
        _stop.set()
        status_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await status_task
        state["connection_state"] = "STOPPED"
        _save_checkpoint(state, recent)
        _write_status(state, connection_state="STOPPED")

    return 0


def _request_stop(*_: Any) -> None:
    _stop.set()


def main() -> int:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass
    return asyncio.run(run_forever())


if __name__ == "__main__":
    import contextlib
    raise SystemExit(main())
