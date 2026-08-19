#!/usr/bin/env python3
"""TradingCore XRP Regime Gate Lab V1.

The 3-year challenger validator found XRPUSDT 4h panic mean-reversion
positive overall on Binance and OKX, but only two of four chronological
segments were positive. This lab tests a small frozen set of economically
motivated pre-signal regime gates over five years, selects only on the first
four years, and evaluates the selected gate once on an untouched final-year
holdout.

Research/PAPER only. No private API, balances, credentials, or order path.
Entries occur at the next bar open; costs and same-bar ambiguity are
conservative.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import challenger_multiyear_validator as base
from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from config.startup_safety import assert_safe_startup
from strategy_atlas_price_v2 import features, fin, sig

SCHEMA = "TRADINGCORE_XRP_REGIME_GATE_V1"
YEARS = 5
TF = "4h"
XRP = "XRPUSDT"
BTC = "BTCUSDT"
CAPITAL_USD = 1000.0
RISK_USD = 1.0
HOLDOUT_DAYS = 365
PURGE_HOURS = 48

# Frozen before observing V1 output. These are simple regime hypotheses,
# not a parameter grid. Every input is known at the signal-bar close.
GATES = (
    "BASE",
    "XRP_E50_GT_E200",
    "XRP_CLOSE_GT_E200",
    "BTC_E50_GT_E200",
    "XRP_AND_BTC_TREND",
    "STRONG_RECLAIM",
    "XRP_TREND_STRONG_RECLAIM",
    "BTC_TREND_STRONG_RECLAIM",
    "XRP_BTC_TREND_STRONG_RECLAIM",
)


@dataclass(frozen=True)
class Window:
    name: str
    start_ms: int
    end_ms: int


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def close_location(candle: Any) -> float | None:
    width = float(candle.high) - float(candle.low)
    if width <= 0:
        return None
    return (float(candle.close) - float(candle.low)) / width


def gate_passes(gate: str, candle: Any, xrp_f: dict[str, Any], btc_f: dict[str, Any] | None) -> bool:
    xrp_trend = (
        all(finite(xrp_f.get(k)) for k in ("e50", "e200"))
        and float(xrp_f["e50"]) > float(xrp_f["e200"])
    )
    xrp_above = finite(xrp_f.get("e200")) and float(candle.close) > float(xrp_f["e200"])
    btc_trend = bool(
        btc_f
        and all(finite(btc_f.get(k)) for k in ("e50", "e200"))
        and float(btc_f["e50"]) > float(btc_f["e200"])
    )
    location = close_location(candle)
    strong_reclaim = finite(location) and float(location) >= (2.0 / 3.0)
    return bool(
        {
            "BASE": True,
            "XRP_E50_GT_E200": xrp_trend,
            "XRP_CLOSE_GT_E200": xrp_above,
            "BTC_E50_GT_E200": btc_trend,
            "XRP_AND_BTC_TREND": xrp_trend and btc_trend,
            "STRONG_RECLAIM": strong_reclaim,
            "XRP_TREND_STRONG_RECLAIM": xrp_trend and strong_reclaim,
            "BTC_TREND_STRONG_RECLAIM": btc_trend and strong_reclaim,
            "XRP_BTC_TREND_STRONG_RECLAIM": xrp_trend and btc_trend and strong_reclaim,
        }[gate]
    )


def stats(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = peak = max_dd = 0.0
    for r_value in rs:
        equity += r_value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(100 * len(wins) / n, 2) if n else None,
        "net_r": round(sum(rs), 4) if n else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else (99.0 if wins else None),
        "expectancy_r": round(sum(rs) / n, 4) if n else None,
        "max_drawdown_r": round(max_dd, 4) if n else None,
    }


def simulate_window(
    gate: str,
    xrp_rows: list[Any],
    btc_feature_by_ts: dict[int, dict[str, Any]],
    signal_window: Window,
) -> dict[str, Any]:
    xrp_features, hours = features(xrp_rows, TF)
    config = TradingCostConfig()
    position: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    rs: list[float] = []
    trades: list[dict[str, Any]] = []
    signals = 0
    max_bars = max(2, int(24 // hours))

    for index, candle in enumerate(xrp_rows):
        timestamp = int(candle.open_time_ms)

        # Signal is known only after its bar closes. Entry is next-bar open.
        if pending is not None and position is None:
            atr_value = float(pending["atr"])
            entry = float(candle.open)
            stop = entry - 1.5 * atr_value
            target = entry + 3.0 * atr_value
            unit_risk = entry - stop
            if stop > 0 and unit_risk > 0:
                quantity = min(RISK_USD / unit_risk, CAPITAL_USD / entry)
                risk = quantity * unit_risk
                if quantity > 0 and risk > 0:
                    position = {
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "quantity": quantity,
                        "risk": risk,
                        "entry_index": index,
                        "signal_ts": int(pending["signal_ts"]),
                    }
            pending = None

        if position is not None:
            exit_price: float | None = None
            reason: str | None = None
            # Adverse gap/stop first, then target, then time exit.
            if float(candle.open) <= float(position["stop"]):
                exit_price, reason = float(candle.open), "GAP_STOP"
            elif float(candle.low) <= float(position["stop"]):
                exit_price, reason = float(position["stop"]), "STOP"
            elif float(candle.open) >= float(position["target"]):
                exit_price, reason = float(candle.open), "GAP_TARGET"
            elif float(candle.high) >= float(position["target"]):
                exit_price, reason = float(position["target"]), "TARGET"
            elif index - int(position["entry_index"]) >= max_bars:
                exit_price, reason = float(candle.close), "TIME"

            if exit_price is not None:
                costs = compute_trade_costs(
                    entry_price=float(position["entry"]),
                    exit_price=exit_price,
                    quantity=float(position["quantity"]),
                    side="LONG",
                    config=config,
                )
                r_value = float(costs["net_pnl"]) / float(position["risk"])
                rs.append(r_value)
                trades.append(
                    {
                        "signal_utc": datetime.fromtimestamp(int(position["signal_ts"]) / 1000, tz=timezone.utc).isoformat(),
                        "exit_utc": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat(),
                        "r": round(r_value, 5),
                        "reason": reason,
                    }
                )
                position = None

        if (
            index + 1 < len(xrp_rows)
            and position is None
            and pending is None
            and signal_window.start_ms <= timestamp <= signal_window.end_ms
            and sig("RSI_PANIC_MEAN_REVERSION", xrp_rows, xrp_features, index, hours)
        ):
            btc_f = btc_feature_by_ts.get(timestamp)
            if gate_passes(gate, candle, xrp_features[index], btc_f):
                atr_value = xrp_features[index].get("atr")
                if fin(atr_value) and float(atr_value) > 0:
                    signals += 1
                    pending = {"atr": float(atr_value), "signal_ts": timestamp}

    return {
        "window": signal_window.name,
        "signals": signals,
        "stats": stats(rs),
        "recent_trades": trades[-12:],
    }


def discovery_checks(full: dict[str, Any], positive_segments: int) -> dict[str, bool]:
    row = full["stats"]
    return {
        "trades": int(row.get("closed_trades") or 0) >= 12,
        "profit_factor": finite(row.get("profit_factor")) and float(row["profit_factor"]) >= 1.15,
        "expectancy": finite(row.get("expectancy_r")) and float(row["expectancy_r"]) >= 0.05,
        "drawdown": finite(row.get("max_drawdown_r")) and float(row["max_drawdown_r"]) <= 10.0,
        "time_segments": positive_segments >= 3,
    }


def holdout_checks(full: dict[str, Any]) -> dict[str, bool]:
    row = full["stats"]
    return {
        "trades": int(row.get("closed_trades") or 0) >= 4,
        "profit_factor": finite(row.get("profit_factor")) and float(row["profit_factor"]) > 1.0,
        "expectancy": finite(row.get("expectancy_r")) and float(row["expectancy_r"]) > 0.0,
        "net_positive": finite(row.get("net_r")) and float(row["net_r"]) > 0.0,
        "drawdown": finite(row.get("max_drawdown_r")) and float(row["max_drawdown_r"]) <= 5.0,
    }


def rank_gate(payload: dict[str, Any]) -> tuple[float, float, int]:
    expectancies: list[float] = []
    profit_factors: list[float] = []
    trade_counts: list[int] = []
    for venue in ("BINANCE", "OKX"):
        row = payload["venues"][venue]["discovery"]["stats"]
        expectancies.append(float(row["expectancy_r"]) if finite(row.get("expectancy_r")) else -999.0)
        profit_factors.append(float(row["profit_factor"]) if finite(row.get("profit_factor")) else -999.0)
        trade_counts.append(int(row.get("closed_trades") or 0))
    return min(expectancies), min(profit_factors), min(trade_counts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="xrp_regime_runtime")
    args = parser.parse_args()
    safety = assert_safe_startup()

    # Reuse audited public candle loaders, extending their window to five years.
    base.YEARS = YEARS
    market: dict[tuple[str, str], list[Any]] = {}
    failures: dict[str, str] = {}
    for venue, loader in (("BINANCE", base.fetch_binance), ("OKX", base.fetch_okx)):
        for symbol in (XRP, BTC):
            try:
                rows = loader(symbol, TF)
                market[(venue, symbol)] = rows
                print("XRP_REGIME_DATA", venue, symbol, "bars=", len(rows), flush=True)
            except Exception as exc:
                failures[f"{venue}:{symbol}:{TF}"] = f"{type(exc).__name__}: {exc}"
                print("XRP_REGIME_DATA_FAIL", venue, symbol, exc, flush=True)

    available_xrp = [rows for (venue, symbol), rows in market.items() if symbol == XRP and rows]
    if not available_xrp:
        raise RuntimeError("No XRP market data available")

    common_start = max(int(rows[0].open_time_ms) for rows in available_xrp)
    common_end = min(int(rows[-1].open_time_ms) for rows in available_xrp)
    holdout_start = common_end - HOLDOUT_DAYS * base.DAY
    purge_ms = PURGE_HOURS * 60 * 60 * 1000
    discovery = Window("DISCOVERY", common_start, holdout_start - purge_ms - 1)
    holdout = Window("HOLDOUT", holdout_start + purge_ms, common_end)

    segment_width = discovery.end_ms - discovery.start_ms + 1
    discovery_segments: list[Window] = []
    for index in range(4):
        segment_start = discovery.start_ms + (segment_width * index) // 4
        segment_end = discovery.start_ms + (segment_width * (index + 1)) // 4 - 1
        discovery_segments.append(Window(f"DISCOVERY_SEGMENT_{index + 1}", segment_start, segment_end))

    results: list[dict[str, Any]] = []
    for gate in GATES:
        venue_payload: dict[str, Any] = {}
        discovery_passed_both = True
        for venue in ("BINANCE", "OKX"):
            xrp_rows = market.get((venue, XRP)) or []
            btc_rows = market.get((venue, BTC)) or []
            if len(xrp_rows) < 1000 or len(btc_rows) < 1000:
                venue_payload[venue] = {
                    "error": "INSUFFICIENT_DATA",
                    "discovery_passed": False,
                    "holdout_passed": False,
                }
                discovery_passed_both = False
                continue

            btc_features, _ = features(btc_rows, TF)
            btc_by_ts = {int(row.open_time_ms): btc_features[i] for i, row in enumerate(btc_rows)}
            discovery_full = simulate_window(gate, xrp_rows, btc_by_ts, discovery)
            segments = [simulate_window(gate, xrp_rows, btc_by_ts, window) for window in discovery_segments]
            positive_segments = sum(
                1
                for segment in segments
                if finite(segment["stats"].get("expectancy_r"))
                and float(segment["stats"]["expectancy_r"]) > 0
            )
            discovery_gate_checks = discovery_checks(discovery_full, positive_segments)
            holdout_full = simulate_window(gate, xrp_rows, btc_by_ts, holdout)
            holdout_gate_checks = holdout_checks(holdout_full)
            venue_payload[venue] = {
                "discovery": discovery_full,
                "discovery_segments": segments,
                "positive_discovery_segments": positive_segments,
                "discovery_checks": discovery_gate_checks,
                "discovery_passed": all(discovery_gate_checks.values()),
                "holdout": holdout_full,
                "holdout_checks": holdout_gate_checks,
                "holdout_passed": all(holdout_gate_checks.values()),
            }
            discovery_passed_both = discovery_passed_both and all(discovery_gate_checks.values())

        results.append(
            {
                "gate": gate,
                "venues": venue_payload,
                "discovery_passed_both_venues": discovery_passed_both,
            }
        )

    discovery_candidates = [row for row in results if row["discovery_passed_both_venues"]]
    discovery_candidates.sort(key=rank_gate, reverse=True)
    selected = discovery_candidates[0] if discovery_candidates else None
    selected_gate = selected["gate"] if selected else None
    holdout_passed_both = bool(
        selected
        and all(bool(selected["venues"][venue].get("holdout_passed")) for venue in ("BINANCE", "OKX"))
    )

    if selected is None:
        state = "NO_XRP_REGIME_CANDIDATE"
    elif holdout_passed_both:
        state = "XRP_REGIME_CANDIDATE_FOUND_FORWARD_REQUIRED"
    else:
        state = "XRP_DISCOVERY_CANDIDATE_FAILED_HOLDOUT"

    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "history_years": YEARS,
        "holdout_days": HOLDOUT_DAYS,
        "purge_hours": PURGE_HOURS,
        "frozen_gates": list(GATES),
        "selection_rule": "Select once using discovery only; evaluate selected gate once on final-year holdout across Binance and OKX.",
        "execution_model": "next-bar-open; conservative TradingCore costs; adverse stop first; spot long-only; 1x capital cap",
        "discovery_window_utc": {
            "start": datetime.fromtimestamp(discovery.start_ms / 1000, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(discovery.end_ms / 1000, tz=timezone.utc).isoformat(),
        },
        "holdout_window_utc": {
            "start": datetime.fromtimestamp(holdout.start_ms / 1000, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(holdout.end_ms / 1000, tz=timezone.utc).isoformat(),
        },
        "selected_gate": selected_gate,
        "selected_holdout_passed_both_venues": holdout_passed_both,
        "discovery_candidates": [row["gate"] for row in discovery_candidates],
        "results": results,
        "data_failures": failures,
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "live_permission": False,
        "note": "A historical+holdout pass is still not LIVE permission. A fresh frozen forward test is mandatory before micro-live review.",
    }
    output_dir = Path(args.output_dir)
    atomic_json(output_dir / "XRP_REGIME_GATE_RESULT.json", report)
    print("=" * 96)
    print("XRP REGIME GATE FINAL")
    print("state:", state)
    print("selected:", selected_gate)
    print("holdout both venues:", holdout_passed_both)
    print("discovery candidates:", report["discovery_candidates"])
    print("LIVE / real orders: DISABLED")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
