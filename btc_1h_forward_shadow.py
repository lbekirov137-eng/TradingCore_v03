#!/usr/bin/env python3
"""
Frozen BTCUSDT 1H forward-shadow challenger.

RESEARCH / PAPER ONLY.

This process starts collecting NEW forward evidence only after the timeframe
choice was frozen by strategy_research_team.py. It does not read API keys,
does not import any order client, does not modify the current PAPER champion,
and has no LIVE execution path.

Frozen evidence before forward run:
- strategy family: SESSION_VWAP_RANGE_LOW_VOL_PX
- symbol: BTCUSDT
- execution timeframe: 1h
- context timeframe: 4H
- strict final holdout start: 2024-02-27T14:00:00+00:00
- strict final holdout trades observed at freeze: 23
- strategy choice frozen on 2026-08-14T12:17:22.999956+00:00

The historical promotion gate requires 30 OOS trades. Therefore seven new
closed FORWARD trades are the minimum count needed before the frozen OOS
sample can be re-evaluated. Count alone is never enough: all economics and
safety gates must be recomputed after the forward extension.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.providers.binance_public_market_provider import BinancePublicMarketProvider
from api.strategy_engine.strategies.contracts import Candle
from api.strategy_engine.strategies.v4_precision import (
    PrecisionConfig,
    SessionVwapRangeLowVolPrecision,
)
from config.startup_safety import assert_safe_startup


SCHEMA = "TRADINGCORE_BTC_1H_FORWARD_SHADOW_V1"
SYMBOL = "BTCUSDT"
EXECUTION_TIMEFRAME = "1h"
CONTEXT_TIMEFRAME = "4h"
STRATEGY_KEY = "SESSION_VWAP_RANGE_LOW_VOL_PX_1H"
STRATEGY_VERSION = "5.0.0-forward-frozen"

# The freeze timestamp is the boundary that makes future observations genuinely
# forward. No candle closing at/before this instant is counted by this process.
FORWARD_FREEZE_UTC = "2026-08-14T12:17:22.999956+00:00"
FORWARD_FREEZE_MS = int(
    datetime.fromisoformat(FORWARD_FREEZE_UTC).timestamp() * 1000
)

HISTORICAL_HOLDOUT_START_UTC = "2024-02-27T14:00:00+00:00"
HISTORICAL_UNTOUCHED_HOLDOUT_TRADES = 23
PROMOTION_MIN_OOS_TRADES = 30
FORWARD_CLOSED_TARGET = (
    PROMOTION_MIN_OOS_TRADES - HISTORICAL_UNTOUCHED_HOLDOUT_TRADES
)

# Must match strategy_research_team.py frozen 1h runner.
MAX_BARS_IN_TRADE = 24
EXECUTION_LIMIT = 999
CONTEXT_LIMIT = 500
DEFAULT_POLL_SECONDS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def data_dir() -> Path:
    value = os.getenv("BTC_1H_SHADOW_DATA_DIR", "C:/TradingCore_BTC_1H_SHADOW")
    return Path(value)


def state_path() -> Path:
    return data_dir() / "state.json"


def journal_path() -> Path:
    return data_dir() / "forward_journal.jsonl"


def status_path() -> Path:
    return data_dir() / "status.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
    temporary.replace(path)


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "symbol": SYMBOL,
        "strategy_key": STRATEGY_KEY,
        "strategy_version": STRATEGY_VERSION,
        "forward_freeze_utc": FORWARD_FREEZE_UTC,
        "historical_holdout_start_utc": HISTORICAL_HOLDOUT_START_UTC,
        "last_processed_open_time_ms": None,
        "last_processed_close_time_ms": None,
        "position": None,
        "forward_closed_trades": 0,
        "forward_wins": 0,
        "forward_losses": 0,
        "forward_net_pnl": 0.0,
        "forward_r_multiples": [],
        "last_event": "INITIALIZED",
        "last_error": None,
        "updated_at_utc": utc_now(),
        "real_order_sent": False,
    }


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return default_state()

    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)

    if not isinstance(state, dict):
        raise TypeError("Forward shadow state must be a dictionary")

    if state.get("schema") != SCHEMA:
        raise RuntimeError("Forward shadow state schema mismatch")

    if state.get("symbol") != SYMBOL or state.get("strategy_key") != STRATEGY_KEY:
        raise RuntimeError("Forward shadow state belongs to a different frozen candidate")

    if state.get("real_order_sent") is not False:
        raise RuntimeError("Unsafe forward state: real_order_sent is not False")

    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at_utc"] = utc_now()
    state["real_order_sent"] = False
    _atomic_json(state_path(), state)


def append_journal(record: dict[str, Any]) -> None:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str))
        handle.write("\n")


def _market_to_candles(market: dict[str, Any]) -> list[Candle]:
    arrays = (
        market["open_times_ms"], market["opens"], market["highs"],
        market["lows"], market["closes"], market["base_volumes"],
    )
    size = min(len(item) for item in arrays)
    candles: list[Candle] = []
    for index in range(size):
        candles.append(
            Candle(
                open_time_ms=int(market["open_times_ms"][index]),
                open=float(market["opens"][index]),
                high=float(market["highs"][index]),
                low=float(market["lows"][index]),
                close=float(market["closes"][index]),
                volume=float(market["base_volumes"][index]),
            )
        )
    return candles


def make_frozen_strategy() -> SessionVwapRangeLowVolPrecision:
    config = PrecisionConfig(
        execution_timeframe="1h",
        context_timeframe="4H",
    )
    strategy = SessionVwapRangeLowVolPrecision(config=config, symbol=SYMBOL)
    strategy.strategy_key = STRATEGY_KEY
    strategy.version = STRATEGY_VERSION
    return strategy


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _build_status(state: dict[str, Any], *, running: bool, detail: str | None = None) -> dict[str, Any]:
    forward_closed = int(state.get("forward_closed_trades") or 0)
    combined_reference = HISTORICAL_UNTOUCHED_HOLDOUT_TRADES + forward_closed
    remaining = max(0, PROMOTION_MIN_OOS_TRADES - combined_reference)

    r_values = [
        float(item) for item in (state.get("forward_r_multiples") or [])
        if isinstance(item, (int, float)) and math.isfinite(float(item))
    ]

    return {
        "schema": SCHEMA,
        "running": running,
        "detail": detail,
        "mode": "FORWARD_SHADOW_PAPER",
        "real_orders_enabled": False,
        "real_order_sent": False,
        "market_data": "BINANCE_PUBLIC_NO_API_KEY",
        "symbol": SYMBOL,
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "context_timeframe": CONTEXT_TIMEFRAME,
        "strategy_key": STRATEGY_KEY,
        "strategy_version": STRATEGY_VERSION,
        "forward_freeze_utc": FORWARD_FREEZE_UTC,
        "historical_untouched_holdout_trades_reference": HISTORICAL_UNTOUCHED_HOLDOUT_TRADES,
        "forward_closed_trades": forward_closed,
        "combined_oos_count_reference": combined_reference,
        "promotion_min_oos_trades": PROMOTION_MIN_OOS_TRADES,
        "additional_closed_trades_needed_for_count_gate": remaining,
        "count_gate_reached": remaining == 0,
        "note": (
            "Count gate alone never promotes the strategy. When reached, the full frozen "
            "holdout plus forward extension must be re-run through every promotion gate."
        ),
        "forward_net_pnl": round(float(state.get("forward_net_pnl") or 0.0), 8),
        "forward_average_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "forward_wins": int(state.get("forward_wins") or 0),
        "forward_losses": int(state.get("forward_losses") or 0),
        "position": state.get("position"),
        "last_event": state.get("last_event"),
        "last_error": state.get("last_error"),
        "last_processed_open_time_ms": state.get("last_processed_open_time_ms"),
        "updated_at_utc": utc_now(),
    }


def write_status(state: dict[str, Any], *, running: bool, detail: str | None = None) -> dict[str, Any]:
    payload = _build_status(state, running=running, detail=detail)
    _atomic_json(status_path(), payload)
    return payload


def _record(
    *,
    candle: Candle,
    close_time_ms: int,
    event: str,
    state: dict[str, Any],
    decision: Any | None = None,
    trade_result: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": SCHEMA,
        "recorded_at_utc": utc_now(),
        "forward_freeze_utc": FORWARD_FREEZE_UTC,
        "exchange": "binance",
        "symbol": SYMBOL,
        "timeframe": EXECUTION_TIMEFRAME,
        "context_timeframe": CONTEXT_TIMEFRAME,
        "strategy_key": STRATEGY_KEY,
        "strategy_version": STRATEGY_VERSION,
        "candle_open_time_ms": int(candle.open_time_ms),
        "candle_close_time_ms": int(close_time_ms),
        "market_price": float(candle.close),
        "candle_high": float(candle.high),
        "candle_low": float(candle.low),
        "event": event,
        "reason": reason,
        "position": state.get("position"),
        "decision": decision.to_dict() if decision is not None else None,
        "trade_result": trade_result,
        "real_order_sent": False,
    }
    return payload


def _close_position(
    state: dict[str, Any],
    candle: Candle,
    *,
    exit_price: float,
    exit_reason: str,
) -> dict[str, Any]:
    position = state.get("position")
    if not isinstance(position, dict):
        raise RuntimeError("Cannot close: no shadow position")

    config = TradingCostConfig()
    costs = compute_trade_costs(
        entry_price=float(position["entry"]),
        exit_price=float(exit_price),
        quantity=float(position["quantity"]),
        side="LONG",
        config=config,
    )

    risk_amount = float(position["risk_amount"])
    r_multiple = costs["net_pnl"] / risk_amount if risk_amount > 0 else None

    result = {
        "event": "POSITION_CLOSED",
        "exit_reason": exit_reason,
        "entry": float(position["entry"]),
        "exit_price": float(exit_price),
        "quantity": float(position["quantity"]),
        "risk_amount": risk_amount,
        "bars_held": int(position.get("bars_held") or 0),
        "gross_pnl": costs["gross_pnl"],
        "net_pnl": costs["net_pnl"],
        "total_fees": costs["total_fees"],
        "slippage_cost": costs["slippage_cost"],
        "r_multiple": r_multiple,
        "cost_config": costs["cost_config"],
        "real_order_sent": False,
    }

    state["forward_closed_trades"] = int(state.get("forward_closed_trades") or 0) + 1
    state["forward_net_pnl"] = float(state.get("forward_net_pnl") or 0.0) + float(costs["net_pnl"])

    values = list(state.get("forward_r_multiples") or [])
    if r_multiple is not None and math.isfinite(float(r_multiple)):
        values.append(float(r_multiple))
    state["forward_r_multiples"] = values

    if costs["net_pnl"] > 0:
        state["forward_wins"] = int(state.get("forward_wins") or 0) + 1
    elif costs["net_pnl"] < 0:
        state["forward_losses"] = int(state.get("forward_losses") or 0) + 1

    state["position"] = None
    state["last_event"] = "POSITION_CLOSED"
    return result


def _manage_existing_position(state: dict[str, Any], candle: Candle) -> dict[str, Any] | None:
    position = state.get("position")
    if not isinstance(position, dict):
        return None

    position["bars_held"] = int(position.get("bars_held") or 0) + 1

    stop = float(position["stop"])
    target = float(position["take_profit_2"])

    # Same conservative ordering as the historical runner: stop before target.
    if float(candle.low) <= stop:
        return _close_position(
            state, candle, exit_price=stop, exit_reason="STOP_LOSS"
        )

    if float(candle.high) >= target:
        return _close_position(
            state, candle, exit_price=target, exit_reason="TAKE_PROFIT_2"
        )

    if int(position["bars_held"]) >= MAX_BARS_IN_TRADE:
        return _close_position(
            state, candle, exit_price=float(candle.close), exit_reason="TIME_STOP"
        )

    position["last_market_price"] = float(candle.close)
    state["position"] = position
    state["last_event"] = "POSITION_REMAINS_OPEN"
    return {
        "event": "POSITION_REMAINS_OPEN",
        "bars_held": int(position["bars_held"]),
        "real_order_sent": False,
    }


def _open_from_decision(state: dict[str, Any], decision: Any, candle: Candle) -> dict[str, Any]:
    diagnostics = decision.diagnostics or {}

    entry = _finite_positive(decision.entry)
    stop = _finite_positive(decision.stop)
    tp1 = _finite_positive(decision.take_profit_1)
    tp2 = _finite_positive(decision.take_profit_2)
    quantity = _finite_positive(diagnostics.get("quantity"))
    risk_amount = _finite_positive(diagnostics.get("actual_risk_usd"))
    leverage = diagnostics.get("leverage")

    if None in (entry, stop, tp1, tp2, quantity, risk_amount):
        raise RuntimeError("Frozen strategy returned incomplete paper geometry")

    if not (stop < entry < tp1 < tp2):
        raise RuntimeError("Frozen strategy returned invalid LONG geometry")

    # Risk can round DOWN below $1 but must never exceed the frozen 0.1%/$1 cap.
    if float(risk_amount) > 1.00000001:
        raise RuntimeError(f"Frozen strategy risk exceeded $1 cap: {risk_amount}")

    if leverage != 1:
        raise RuntimeError(f"Frozen strategy leverage is not 1x: {leverage}")

    # A closed-candle strategy enters at the close used by the decision.
    if abs(float(candle.close) - float(entry)) > 0.011:
        raise RuntimeError(
            f"Decision entry {entry} does not match closed candle {candle.close}"
        )

    position = {
        "status": "OPEN",
        "mode": "PAPER_FORWARD_SHADOW",
        "exchange": "binance",
        "symbol": SYMBOL,
        "timeframe": EXECUTION_TIMEFRAME,
        "strategy_key": STRATEGY_KEY,
        "strategy_version": STRATEGY_VERSION,
        "side": "LONG",
        "entry": float(entry),
        "stop": float(stop),
        "take_profit_1": float(tp1),
        "take_profit_2": float(tp2),
        "quantity": float(quantity),
        "risk_amount": float(risk_amount),
        "bars_held": 0,
        "opened_candle_open_time_ms": int(candle.open_time_ms),
        "opened_at_utc": utc_now(),
        "last_market_price": float(candle.close),
        "real_order_sent": False,
    }
    state["position"] = position
    state["last_event"] = "POSITION_OPENED"
    return {
        "event": "POSITION_OPENED",
        "position": position,
        "real_order_sent": False,
    }


def process_available_closed_candles(state: dict[str, Any]) -> int:
    execution_market = BinancePublicMarketProvider.fetch(
        symbol=SYMBOL,
        interval=EXECUTION_TIMEFRAME,
        limit=EXECUTION_LIMIT,
    )
    context_market = BinancePublicMarketProvider.fetch(
        symbol=SYMBOL,
        interval=CONTEXT_TIMEFRAME,
        limit=CONTEXT_LIMIT,
    )

    execution = _market_to_candles(execution_market)
    context = _market_to_candles(context_market)
    close_times = [int(value) for value in execution_market["close_times_ms"]]

    if len(execution) != len(close_times):
        raise RuntimeError("Execution candle/close-time length mismatch")

    last_processed = state.get("last_processed_open_time_ms")
    if last_processed is not None:
        first_available = int(execution[0].open_time_ms)
        if int(last_processed) < first_available:
            raise RuntimeError(
                "FORWARD_HISTORY_GAP: last processed candle is older than the public "
                "provider window; reconcile before continuing"
            )

    strategy = make_frozen_strategy()
    processed = 0

    for index, candle in enumerate(execution):
        close_time_ms = close_times[index]

        # Nothing at or before the strategy freeze is forward evidence.
        if close_time_ms <= FORWARD_FREEZE_MS:
            continue

        if last_processed is not None and int(candle.open_time_ms) <= int(last_processed):
            continue

        trade_result = _manage_existing_position(state, candle)

        # Match the historical runner: if a position closes on this candle,
        # the same closed candle may also be evaluated for a new setup.
        decision = None
        event = trade_result["event"] if trade_result else "NO_POSITION"
        reason = None

        if state.get("position") is None:
            decision = strategy.evaluate_with_context(execution, index, context)

            if decision.is_trade:
                opened = _open_from_decision(state, decision, candle)
                if trade_result is None:
                    trade_result = opened
                    event = opened["event"]
                else:
                    trade_result = {
                        "closed": trade_result,
                        "opened": opened,
                        "real_order_sent": False,
                    }
                    event = "POSITION_CLOSED_AND_REOPENED"
            else:
                if trade_result is None:
                    event = "NO_POSITION_OPENED"
                    reason = decision.reason_code
                    state["last_event"] = event

        record = _record(
            candle=candle,
            close_time_ms=close_time_ms,
            event=event,
            state=state,
            decision=decision,
            trade_result=trade_result,
            reason=reason,
        )
        append_journal(record)

        state["last_processed_open_time_ms"] = int(candle.open_time_ms)
        state["last_processed_close_time_ms"] = int(close_time_ms)
        state["last_error"] = None
        save_state(state)
        processed += 1
        last_processed = int(candle.open_time_ms)

        print(
            "FORWARD_SHADOW "
            f"utc={record['recorded_at_utc']} "
            f"candle={candle.open_time_ms} "
            f"event={event} "
            f"forward_closed={state['forward_closed_trades']} "
            "real_order_sent=False",
            flush=True,
        )

    return processed


def run_forever() -> int:
    safety = assert_safe_startup()
    poll_seconds = _positive_int_env("BTC_1H_SHADOW_POLL_SECONDS", DEFAULT_POLL_SECONDS)
    data_dir().mkdir(parents=True, exist_ok=True)
    state = load_state()

    print("=" * 78)
    print("TRADINGCORE BTCUSDT 1H FROZEN FORWARD SHADOW")
    print("=" * 78)
    print("Safety:", safety)
    print("Strategy:", f"{STRATEGY_KEY}@{STRATEGY_VERSION}")
    print("Forward freeze:", FORWARD_FREEZE_UTC)
    print("Historical holdout trades reference:", HISTORICAL_UNTOUCHED_HOLDOUT_TRADES)
    print("Forward closed target for count gate:", FORWARD_CLOSED_TARGET)
    print("Market data: BINANCE PUBLIC / NO API KEY")
    print("REAL ORDERS: IMPOSSIBLE FROM THIS SCRIPT")
    print("Data:", data_dir())
    print("=" * 78, flush=True)

    write_status(state, running=True, detail="STARTED")

    while True:
        try:
            processed = process_available_closed_candles(state)
            status = write_status(
                state,
                running=True,
                detail=(
                    f"processed {processed} new closed candle(s)"
                    if processed
                    else "waiting for next closed 1h candle"
                ),
            )

            print(
                "FORWARD_STATUS "
                f"closed={status['forward_closed_trades']} "
                f"combined_count_reference={status['combined_oos_count_reference']} "
                f"remaining={status['additional_closed_trades_needed_for_count_gate']} "
                f"position={'OPEN' if status['position'] else 'NONE'} "
                "real_orders=False",
                flush=True,
            )

        except KeyboardInterrupt:
            state["last_event"] = "STOPPED_BY_USER"
            save_state(state)
            write_status(state, running=False, detail="STOPPED_BY_USER")
            return 0

        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            state["last_error"] = detail
            state["last_event"] = "FAILED_SAFELY"
            save_state(state)
            write_status(state, running=True, detail="FAILED_SAFELY; retrying")
            print(
                f"[FORWARD SHADOW] FAILED_SAFELY: {detail}",
                file=sys.stderr,
                flush=True,
            )

        time.sleep(poll_seconds)


def main() -> int:
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
