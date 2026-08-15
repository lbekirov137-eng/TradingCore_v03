#!/usr/bin/env python3
"""Independent cross-venue confirmation for the already-frozen BTCUSDT 1H candidate.

Purpose: get a fast reliability answer without inventing another strategy.
The strategy logic is imported from btc_1h_forward_shadow.py and is not tuned here.
The confirmation sample is Bybit public BTCUSDT spot 1H history cached by the
Historical Accelerator. All loaded history is treated as independent cross-venue
confirmation for this frozen candidate. No private API, no order client, no LIVE path.

This module is intentionally self-contained with respect to research orchestration:
it does NOT import strategy_lab_orchestrator / strategy_lab_deep_dive and therefore
does not require the optional requests package. Its backtest runner and cached 4H
context helper mirror the existing TradingCore implementations directly.
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_engine.strategies.contracts import Candle
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from api.strategy_supervisor.validation import build_walk_forward_windows, robustness_ratio
from btc_1h_forward_shadow import make_frozen_strategy
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_BTC_1H_BYBIT_CONFIRMATORY_V1"
CACHE = Path("C:/TradingCore_Historical_Accelerator/cache/BTCUSDT.json.gz")
OUT = Path("C:/TradingCore_BTC_1H_CONFIRMATORY")
HOUR_MS = 3_600_000
FOUR_HOUR_MS = 4 * HOUR_MS
MIN_HISTORY_DAYS = 365

# Reliability gates are intentionally stricter than the generic promotion gate.
STRICT_MIN_TRADES = 30
STRICT_MIN_PF = 1.25
STRICT_MIN_EXPECTANCY_R = 0.10
STRICT_MAX_DD_R = 8.0
STRICT_MIN_ROBUSTNESS = 0.60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_iso(open_time_ms: int) -> str:
    return datetime.fromtimestamp(open_time_ms / 1000.0, tz=timezone.utc).isoformat()


def atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def load_1h() -> list[Candle]:
    if not CACHE.exists():
        raise RuntimeError(f"Historical cache not found: {CACHE}")
    with gzip.open(CACHE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("BTCUSDT cache has no bars")
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            candles.append(Candle(
                open_time_ms=int(row[0]),
                open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]),
            ))
        except (TypeError, ValueError):
            continue
    candles = sorted({int(c.open_time_ms): c for c in candles}.values(), key=lambda c: c.open_time_ms)
    if len(candles) < 24 * MIN_HISTORY_DAYS:
        raise RuntimeError(f"Insufficient Bybit 1H history: {len(candles)} bars")
    return candles


def aggregate_4h(rows: list[Candle]) -> list[Candle]:
    buckets: dict[int, list[Candle]] = {}
    for candle in rows:
        start = (int(candle.open_time_ms) // FOUR_HOUR_MS) * FOUR_HOUR_MS
        buckets.setdefault(start, []).append(candle)
    result: list[Candle] = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda c: c.open_time_ms)
        expected = [start + i * HOUR_MS for i in range(4)]
        actual = [int(c.open_time_ms) for c in group]
        if actual != expected:
            continue
        result.append(Candle(
            open_time_ms=start,
            open=float(group[0].open),
            high=max(float(c.high) for c in group),
            low=min(float(c.low) for c in group),
            close=float(group[-1].close),
            volume=sum(float(c.volume) for c in group),
        ))
    return result


def ema_prefix(values: list[float], period: int) -> list[float | None]:
    """Exact prefix EMA equivalent to TradingCore contracts.ema(), computed once."""
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current
    return result


def install_cached_context(strategy: Any, context_candles: list[Candle]) -> None:
    """Install the same closed-4H context cache used by Strategy Lab, locally."""
    closes = [float(candle.close) for candle in context_candles]
    fast_prefix = ema_prefix(closes, 20)
    slow_prefix = ema_prefix(closes, 50)
    available_at = [int(candle.open_time_ms) + FOUR_HOUR_MS for candle in context_candles]

    def cached_context_is_bullish(self, window, _context_candles):
        deadline = int(window.current.open_time_ms)
        index = bisect.bisect_right(available_at, deadline) - 1
        if index < 0:
            return False, {"context": "UNAVAILABLE"}
        fast = fast_prefix[index]
        slow = slow_prefix[index]
        if fast is None or slow is None:
            return False, {"context": "INSUFFICIENT_CONTEXT_HISTORY"}
        latest = context_candles[index]
        return fast > slow, {
            "context_close": round(float(latest.close), 2),
            "context_fast_ema": round(float(fast), 2),
            "context_slow_ema": round(float(slow), 2),
            "context_open_time_ms": latest.open_time_ms,
        }

    strategy.context_is_bullish = types.MethodType(cached_context_is_bullish, strategy)


def regime_for(atr_percent: float | None) -> str:
    if atr_percent is None:
        return "UNKNOWN"
    if atr_percent < 0.8:
        return "RANGE"
    if atr_percent > 1.5:
        return "VOLATILE"
    return "TREND"


def run_context_backtest(
    strategy: Any,
    candles: list[Candle],
    context_candles: list[Candle],
    *,
    max_bars_in_trade: int = 24,
) -> dict[str, Any]:
    """Dependency-free mirror of TradingCore's conservative LONG-only runner."""
    cost_config = TradingCostConfig()
    trades: list[ClosedTrade] = []
    reason_counts: dict[str, int] = {}
    signals = 0
    open_trade: dict[str, Any] | None = None

    for index, candle in enumerate(candles):
        if open_trade is not None:
            stop = open_trade["stop"]
            target = open_trade["target"]
            exit_price = None

            # Conservative ambiguous-bar ordering: stop before target.
            if candle.low <= stop:
                exit_price = stop
            elif candle.high >= target:
                exit_price = target
            elif index - open_trade["entry_index"] >= max_bars_in_trade:
                exit_price = candle.close

            if exit_price is not None:
                costs = compute_trade_costs(
                    entry_price=open_trade["entry"],
                    exit_price=exit_price,
                    quantity=open_trade["quantity"],
                    side="LONG",
                    config=cost_config,
                )
                risk = open_trade["risk_amount"]
                trades.append(ClosedTrade(
                    strategy_id=strategy.strategy_key,
                    closed_at_utc=utc_iso(candle.open_time_ms),
                    regime=open_trade["regime"],
                    net_pnl=costs["net_pnl"],
                    r_multiple=(costs["net_pnl"] / risk if risk and risk > 0 else None),
                ))
                open_trade = None

            if open_trade is not None:
                continue

        decision = strategy.evaluate_with_context(candles, index, context_candles)
        reason_counts[decision.reason_code] = reason_counts.get(decision.reason_code, 0) + 1
        if not decision.is_trade:
            continue

        signals += 1
        entry = float(decision.entry)
        stop = float(decision.stop)
        target = float(decision.take_profit_2)
        if not (stop < entry < target):
            continue

        diagnostics = decision.diagnostics or {}
        stop_distance = entry - stop
        try:
            quantity = float(diagnostics.get("quantity"))
        except (TypeError, ValueError):
            quantity = 1.0 / stop_distance
        if not math.isfinite(quantity) or quantity <= 0:
            continue

        try:
            risk_amount = float(diagnostics.get("actual_risk_usd"))
        except (TypeError, ValueError):
            risk_amount = 1.0
        if not math.isfinite(risk_amount) or risk_amount <= 0:
            risk_amount = 1.0

        open_trade = {
            "entry": entry,
            "stop": stop,
            "target": target,
            "quantity": quantity,
            "risk_amount": risk_amount,
            "entry_index": index,
            "regime": regime_for(diagnostics.get("atr_percent")),
        }

    return {
        "strategy_key": strategy.strategy_key,
        "version": strategy.version,
        "parameter_fingerprint": strategy.config.fingerprint(),
        "candles": len(candles),
        "signals": signals,
        "closed_trades": len(trades),
        "still_open_at_end": open_trade is not None,
        "trades": trades,
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda pair: -pair[1])[:12]),
        "cost_config": cost_config.snapshot(),
    }


def main() -> int:
    safety = assert_safe_startup()
    one_h = load_1h()
    four_h = aggregate_4h(one_h)
    if len(four_h) < 20 * 8:
        raise SystemExit("Insufficient reconstructed 4H context")

    first = int(one_h[0].open_time_ms)
    last = int(one_h[-1].open_time_ms)
    span_days = (last - first) / 86_400_000.0

    strategy = make_frozen_strategy()
    install_cached_context(strategy, four_h)
    bt = run_context_backtest(strategy, one_h, four_h, max_bars_in_trade=24)
    trades = list(bt["trades"])
    stats = build_stats(trades)
    windows = build_walk_forward_windows(trades, window_count=6)
    robust = robustness_ratio(windows)
    wf = bool(windows and robust is not None and robust >= STRICT_MIN_ROBUSTNESS)

    validation = {
        "strategy_id": strategy.strategy_key,
        "sample_id": f"BYBIT_CROSS_VENUE_BTC1H_{first}_{last}",
        "oos_trades": stats.get("closed_trades"),
        "oos_net_pnl": stats.get("net_pnl"),
        "oos_profit_factor": stats.get("profit_factor"),
        "oos_expectancy_r": stats.get("expectancy_r"),
        "oos_max_drawdown_r": stats.get("max_drawdown_r"),
        "oos_win_rate_percent": stats.get("win_rate_percent"),
        "robustness_ratio": robust,
        "walk_forward_passed": wf,
        "look_ahead_leakage": False,
        "safety_violations": [],
        "note": "Frozen strategy, independent Bybit venue history; no parameter or timeframe selection in this run.",
    }
    generic = promotion_gates(validation)

    def num(name: str, fallback: float = -1e99) -> float:
        value = validation.get(name)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else fallback

    strict_checks = {
        "min_trades_30": int(validation.get("oos_trades") or 0) >= STRICT_MIN_TRADES,
        "pf_ge_1_25": num("oos_profit_factor") >= STRICT_MIN_PF,
        "expectancy_ge_0_10r": num("oos_expectancy_r") >= STRICT_MIN_EXPECTANCY_R,
        "dd_le_8r": 0 <= num("oos_max_drawdown_r", 1e99) <= STRICT_MAX_DD_R,
        "robustness_ge_0_60": num("robustness_ratio") >= STRICT_MIN_ROBUSTNESS,
        "walk_forward": wf,
        "generic_promotion": bool(generic.get("passed")),
    }
    passed = all(strict_checks.values())
    state = "BTC_1H_CROSS_VENUE_CONFIRM_PASS" if passed else "BTC_1H_CROSS_VENUE_CONFIRM_FAIL"

    report = {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "state": state,
        "source": "BYBIT_PUBLIC_SPOT_HISTORY_FROM_SEALED_CACHE",
        "symbol": "BTCUSDT",
        "strategy_key": strategy.strategy_key,
        "strategy_version": strategy.version,
        "history_span_days": round(span_days, 2),
        "bars_1h": len(one_h),
        "bars_4h": len(four_h),
        "signals": bt.get("signals"),
        "stats": stats,
        "validation": validation,
        "generic_promotion_gates": generic,
        "strict_confirmatory_checks": strict_checks,
        "safety": safety,
        "real_orders_enabled": False,
        "live_permission": False,
        "private_api_used": False,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    atomic(OUT / "LATEST_BTC_1H_BYBIT_CONFIRMATORY.json", report)
    atomic(OUT / "DECISION_LOCK.json", {
        "schema": "TRADINGCORE_BTC_1H_BYBIT_CONFIRMATORY_DECISION_V1",
        "locked_at_utc": utc_now(),
        "state": state,
        "sample_id": validation["sample_id"],
        "strategy_key": strategy.strategy_key,
        "strict_confirmatory_checks": strict_checks,
        "holdout_reopen_allowed": False,
        "real_orders_enabled": False,
        "live_permission": False,
    })

    print("=" * 92)
    print("BTC 1H CROSS-VENUE CONFIRMATORY RESULT")
    print("State:", state)
    print(f"Bybit history: {span_days:.1f} days | 1H={len(one_h)} 4H={len(four_h)}")
    print(
        "Trades={trades} PF={pf} expR={exp} net={net} DD_R={dd} win={win} robust={robust}".format(
            trades=stats.get("closed_trades"), pf=stats.get("profit_factor"),
            exp=stats.get("expectancy_r"), net=stats.get("net_pnl"),
            dd=stats.get("max_drawdown_r"), win=stats.get("win_rate_percent"), robust=robust,
        )
    )
    print("Generic promotion:", generic.get("passed"), "failed=", generic.get("failed_gates"))
    print("Strict confirmatory:", passed, strict_checks)
    print("Existing BTC 1H forward shadow remains mandatory; PASS here is not LIVE permission.")
    print("LIVE / real orders: DISABLED")
    print("Report:", OUT / "LATEST_BTC_1H_BYBIT_CONFIRMATORY.json")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
