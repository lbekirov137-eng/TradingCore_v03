#!/usr/bin/env python3
"""Corrected execution wrapper for TradingCore Funding Carry V1.

Fixes two conservative accounting details before the first result is accepted:
1) funding is bucketed by actual 8h settlement interval and is not credited on
   the same bar in which a position is opened;
2) a discovery/segment position is forced out at the window boundary, so no
   holdout price or funding can leak backward into discovery statistics.

All lane definitions, thresholds, costs and selection gates remain frozen in
funding_carry_walkforward.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import funding_carry_walkforward as base
from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs


def finite(value: Any) -> bool:
    return base.finite(value)


def bucketed_rates(market: list[dict[str, Any]], funding: list[tuple[int, float]]) -> list[float | None]:
    buckets: dict[int, float] = {}
    for timestamp, rate in funding:
        if not finite(rate):
            continue
        bucket = (int(timestamp) // base.BAR_MS) * base.BAR_MS
        buckets[bucket] = buckets.get(bucket, 0.0) + float(rate)
    return [buckets.get((int(row["ts"]) // base.BAR_MS) * base.BAR_MS) for row in market]


def simulate(
    lane: dict[str, Any],
    market: list[dict[str, Any]],
    funding: list[tuple[int, float]],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    config = TradingCostConfig()
    rates = bucketed_rates(market, funding)
    position: dict[str, Any] | None = None
    pending_entry = False
    pending_exit = False
    trade_returns: list[float] = []
    trades: list[dict[str, Any]] = []
    signals = 0

    def close_position(exit_spot: float, exit_perp: float, timestamp: int, index: int, reason: str) -> None:
        nonlocal position
        if position is None:
            return
        spot_cost = compute_trade_costs(
            entry_price=float(position["spot_entry"]),
            exit_price=float(exit_spot),
            quantity=float(position["spot_quantity"]),
            side="LONG",
            config=config,
        )
        perp_cost = compute_trade_costs(
            entry_price=float(position["perp_entry"]),
            exit_price=float(exit_perp),
            quantity=float(position["perp_quantity"]),
            side="SHORT",
            config=config,
        )
        total_pnl = (
            float(spot_cost["net_pnl"])
            + float(perp_cost["net_pnl"])
            + float(position["funding_pnl"])
        )
        trade_return = total_pnl / base.CAPITAL_USD
        trade_returns.append(trade_return)
        trades.append(
            {
                "entry_utc": datetime.fromtimestamp(int(position["entry_ts"]) / 1000, tz=timezone.utc).isoformat(),
                "exit_utc": datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc).isoformat(),
                "holding_bars": int(index) - int(position["entry_index"]),
                "funding_pnl": round(float(position["funding_pnl"]), 6),
                "return_pct": round(100 * trade_return, 5),
                "reason": reason,
            }
        )
        position = None

    for index, row in enumerate(market):
        timestamp = int(row["ts"])
        spot_open = float(row["spot"]["open"])
        perp_open = float(row["perp"]["open"])

        # Hard window boundary: discovery and each segment cannot use later prices.
        if timestamp > end_ms:
            pending_entry = False
            pending_exit = False
            if position is not None:
                close_position(spot_open, perp_open, timestamp, index, "WINDOW_BOUNDARY")
            break

        if pending_exit and position is not None:
            close_position(
                spot_open,
                perp_open,
                timestamp,
                index,
                str(position.get("exit_reason") or "EXIT"),
            )
            pending_exit = False

        # Entry is after this timestamp's funding settlement; same-bar funding is
        # deliberately not credited.
        if pending_entry and position is None:
            if timestamp <= end_ms:
                spot_quantity = base.LEG_NOTIONAL_USD / spot_open
                perp_quantity = base.LEG_NOTIONAL_USD / perp_open
                position = {
                    "entry_ts": timestamp,
                    "entry_index": index,
                    "spot_entry": spot_open,
                    "perp_entry": perp_open,
                    "spot_quantity": spot_quantity,
                    "perp_quantity": perp_quantity,
                    "entry_basis": perp_open / spot_open - 1.0,
                    "funding_pnl": 0.0,
                }
            pending_entry = False

        if position is not None:
            # Credit only funding events strictly after entry.
            rate = rates[index]
            if index > int(position["entry_index"]) and finite(rate):
                position["funding_pnl"] = (
                    float(position["funding_pnl"])
                    + base.LEG_NOTIONAL_USD * float(rate)
                )

            current_basis = float(row["perp"]["close"]) / float(row["spot"]["close"]) - 1.0
            lookback = int(lane["lookback"])
            recent = [
                value
                for value in rates[max(0, index - lookback + 1): index + 1]
                if finite(value)
            ]
            rolling = (
                sum(float(value) for value in recent) / len(recent)
                if len(recent) == lookback
                else None
            )
            holding = index - int(position["entry_index"])
            if holding >= base.MAX_HOLD_BARS:
                position["exit_reason"] = "MAX_HOLD"
                pending_exit = True
            elif current_basis - float(position["entry_basis"]) >= base.BASIS_STOP_WIDENING:
                position["exit_reason"] = "BASIS_STOP"
                pending_exit = True
            elif finite(rolling) and float(rolling) <= 0:
                position["exit_reason"] = "FUNDING_DECAY"
                pending_exit = True

        if (
            position is None
            and not pending_entry
            and not pending_exit
            and start_ms <= timestamp <= end_ms
        ):
            lookback = int(lane["lookback"])
            recent = [
                value
                for value in rates[max(0, index - lookback + 1): index + 1]
                if finite(value)
            ]
            rolling = (
                sum(float(value) for value in recent) / len(recent)
                if len(recent) == lookback
                else None
            )
            basis = float(row["basis"])
            if (
                finite(rolling)
                and float(rolling) >= float(lane["threshold"])
                and base.MIN_ENTRY_BASIS <= basis <= base.MAX_ENTRY_BASIS
                and index + 1 < len(market)
                and int(market[index + 1]["ts"]) <= end_ms
            ):
                signals += 1
                pending_entry = True

    # Dataset may end exactly at the holdout boundary; close conservatively at
    # the final known close rather than silently dropping an open trade.
    if position is not None and market:
        last_index = min(len(market) - 1, max(0, len(market) - 1))
        last = market[last_index]
        close_position(
            float(last["spot"]["close"]),
            float(last["perp"]["close"]),
            int(last["ts"]),
            last_index,
            "DATA_END",
        )

    return {
        "signals": signals,
        "stats": base.stats(trade_returns),
        "recent_trades": trades[-15:],
    }


base.simulate = simulate

if __name__ == "__main__":
    raise SystemExit(base.main())
