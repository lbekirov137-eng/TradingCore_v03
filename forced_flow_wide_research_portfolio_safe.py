#!/usr/bin/env python3
"""Portfolio-safe entrypoint for Wide V2 research.

Monkey-patches the preregistered research module's simulator BEFORE main() runs
so historical validation obeys MAX_CONCURRENT_POSITIONS=1 globally. No outcomes
have been opened by Wide V2 before this wrapper was introduced.
"""
from __future__ import annotations

from typing import Any

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_supervisor.stats import ClosedTrade
import forced_flow_wide_protocol as protocol
import forced_flow_wide_research_engine as engine


def simulate_portfolio_safe(
    clusters: list[engine.Cluster],
    prices: dict[str, list[engine.Candle]],
    threshold: float,
) -> tuple[list[ClosedTrade], list[dict[str, Any]]]:
    qualifying = [
        c for c in clusters
        if c.side == protocol.LIQUIDATED_SIDE and c.turnover_bps >= threshold
    ]
    qualifying.sort(key=lambda c: c.end_ms)
    indexes = {
        symbol: {candle.start_ms: i for i, candle in enumerate(rows)}
        for symbol, rows in prices.items()
    }
    global_busy_until = 0
    trades: list[ClosedTrade] = []
    details: list[dict[str, Any]] = []
    costs = TradingCostConfig()

    for cluster in qualifying:
        if cluster.end_ms < global_busy_until:
            continue
        rows = prices.get(cluster.symbol) or []
        stabilisation_start = engine.ceil_minute(cluster.end_ms)
        idx = (indexes.get(cluster.symbol) or {}).get(stabilisation_start)
        if idx is None or idx < protocol.ATR_PERIOD + 1:
            continue
        if not rows[idx].close > rows[idx].open:
            continue
        entry_i = idx + protocol.STABILISATION_CANDLES
        if entry_i >= len(rows):
            continue
        atr = engine.atr14(rows[:idx])
        if atr is None or atr <= 0:
            continue
        entry = rows[entry_i].open
        stop = entry - protocol.ATR_STOP_MULTIPLE * atr
        if stop <= 0 or stop >= entry:
            continue
        risk_unit = entry - stop
        quantity = protocol.RISK_AMOUNT_USD / risk_unit
        position_notional = quantity * entry
        if position_notional > protocol.REFERENCE_CAPITAL_USD * protocol.MAX_LEVERAGE + 1e-9:
            continue
        target = entry + protocol.RISK_REWARD * risk_unit
        end_i = min(len(rows) - 1, entry_i + protocol.MAX_HOLD_MINUTES - 1)
        exit_price = None
        exit_reason = None
        exit_i = None
        for i in range(entry_i, end_i + 1):
            candle = rows[i]
            if candle.low <= stop:
                exit_price, exit_reason, exit_i = stop, "STOP_LOSS", i
                break
            if candle.high >= target:
                exit_price, exit_reason, exit_i = target, "TAKE_PROFIT", i
                break
        if exit_price is None:
            exit_i = end_i
            exit_price = rows[exit_i].close
            exit_reason = "TIME_STOP"

        result = compute_trade_costs(
            entry_price=entry,
            exit_price=float(exit_price),
            quantity=quantity,
            side="LONG",
            config=costs,
        )
        net = float(result["net_pnl"])
        closed_ms = rows[exit_i].start_ms + engine.MINUTE_MS
        trades.append(
            ClosedTrade(
                protocol.STRATEGY_ID,
                engine.utc(closed_ms),
                cluster.symbol,
                net,
                net / protocol.RISK_AMOUNT_USD,
            )
        )
        global_busy_until = closed_ms
        details.append({
            "symbol": cluster.symbol,
            "cluster_end_utc": engine.utc(cluster.end_ms),
            "cluster_events": cluster.event_count,
            "cluster_notional_usdt": round(cluster.notional, 2),
            "cluster_turnover_bps": round(cluster.turnover_bps, 6),
            "threshold_turnover_bps": threshold,
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit": exit_price,
            "exit_reason": exit_reason,
            "net_pnl": net,
            "r_multiple": net / protocol.RISK_AMOUNT_USD,
            "portfolio_concurrency": 1,
            "real_order_sent": False,
        })

    return trades, details


engine.simulate = simulate_portfolio_safe

if __name__ == "__main__":
    raise SystemExit(engine.main())
