#!/usr/bin/env python3
"""TradingCore Microstructure Forward Execution V1.

Evaluates a small set of microstructure hypotheses frozen after discovery.
Signals and entries use only snapshots after the freeze. Entry is delayed to the
next snapshot at the quoted ask; exit is at the future quoted bid. TradingCore's
conservative fees and slippage are applied on top of the observed spread.

Research/PAPER only. No private API, balances, credentials, or order path.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_MICROSTRUCTURE_FORWARD_EXECUTION_V1"
FREEZE_UTC = "2026-08-19T17:05:00+00:00"
CAPITAL_USD = 1000.0
LANES = (
    {"lane_id": "ETH:TOP_HIGH_CONTINUATION:H6", "symbol": "ETH", "signal": "TOP_HIGH_CONTINUATION", "horizon": 6},
    {"lane_id": "BTC:DEPTH_HIGH_CONTINUATION:H6", "symbol": "BTC", "signal": "DEPTH_HIGH_CONTINUATION", "horizon": 6},
    {"lane_id": "ETH:FLOW_SELL_REVERSION_LONG:H6", "symbol": "ETH", "signal": "FLOW_SELL_REVERSION_LONG", "horizon": 6},
    {"lane_id": "SOL:FLOW_SELL_REVERSION_LONG:H3", "symbol": "SOL", "signal": "FLOW_SELL_REVERSION_LONG", "horizon": 3},
    {"lane_id": "BTC:DEPTH_LOW_REVERSION_LONG:H6", "symbol": "BTC", "signal": "DEPTH_LOW_REVERSION_LONG", "horizon": 6},
)


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def parse_time(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def load_snapshots(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
            stamp = payload.get("recorded_at_utc")
            if stamp:
                payload["_ts_ms"] = parse_time(str(stamp))
                rows.append(payload)
        except Exception:
            pass
    rows.sort(key=lambda item: int(item["_ts_ms"]))
    return rows


def symbol_row(snapshot: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    for row in snapshot.get("symbols") or []:
        if str(row.get("symbol")) == symbol:
            return row
    return None


def signal_matches(name: str, row: dict[str, Any]) -> bool:
    depth = row.get("okx_orderbook_imbalance")
    top = row.get("okx_top_level_imbalance")
    flow = (row.get("okx_trade_flow") or {}).get("signed_imbalance")
    if name == "TOP_HIGH_CONTINUATION":
        return finite(top) and float(top) >= 0.80
    if name == "DEPTH_HIGH_CONTINUATION":
        return finite(depth) and float(depth) >= 0.68
    if name == "DEPTH_LOW_REVERSION_LONG":
        return finite(depth) and float(depth) <= 0.32
    if name == "FLOW_SELL_REVERSION_LONG":
        return finite(flow) and float(flow) <= -0.25
    return False


def stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = peak = max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(100 * len(wins) / n, 2) if n else None,
        "net_bps": round(sum(values), 4) if n else None,
        "average_net_bps": round(sum(values) / n, 4) if n else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else (99.0 if wins else None),
        "max_drawdown_bps": round(max_dd, 4) if n else None,
    }


def time_segment_positive(trades: list[dict[str, Any]]) -> int:
    if len(trades) < 8:
        return 0
    positives = 0
    for segment in range(4):
        start = (len(trades) * segment) // 4
        end = (len(trades) * (segment + 1)) // 4
        values = [float(item["net_bps"]) for item in trades[start:end]]
        if values and sum(values) > 0:
            positives += 1
    return positives


def evaluate_lane(lane: dict[str, Any], snapshots: list[dict[str, Any]], freeze_ms: int) -> dict[str, Any]:
    config = TradingCostConfig()
    horizon = int(lane["horizon"])
    symbol = str(lane["symbol"])
    signal_name = str(lane["signal"])
    trades: list[dict[str, Any]] = []
    last_exit_index = -1
    signal_count = 0
    for index, snapshot in enumerate(snapshots):
        if int(snapshot["_ts_ms"]) < freeze_ms or index <= last_exit_index:
            continue
        signal_row = symbol_row(snapshot, symbol)
        if signal_row is None or not signal_matches(signal_name, signal_row):
            continue
        signal_count += 1
        entry_index = index + 1
        exit_index = index + horizon
        if exit_index >= len(snapshots):
            continue
        entry_row = symbol_row(snapshots[entry_index], symbol)
        exit_row = symbol_row(snapshots[exit_index], symbol)
        if entry_row is None or exit_row is None:
            continue
        entry = (entry_row.get("okx_spot") or {}).get("ask")
        exit_price = (exit_row.get("okx_spot") or {}).get("bid")
        if not finite(entry) or not finite(exit_price) or float(entry) <= 0 or float(exit_price) <= 0:
            continue
        quantity = CAPITAL_USD / float(entry)
        costs = compute_trade_costs(
            entry_price=float(entry),
            exit_price=float(exit_price),
            quantity=quantity,
            side="LONG",
            config=config,
        )
        net_return = float(costs["net_pnl"]) / CAPITAL_USD
        net_bps = net_return * 10_000.0
        trades.append(
            {
                "signal_utc": snapshot["recorded_at_utc"],
                "entry_utc": snapshots[entry_index]["recorded_at_utc"],
                "exit_utc": snapshots[exit_index]["recorded_at_utc"],
                "entry_ask": round(float(entry), 10),
                "exit_bid": round(float(exit_price), 10),
                "net_bps": round(net_bps, 5),
            }
        )
        last_exit_index = exit_index
    values = [float(item["net_bps"]) for item in trades]
    result_stats = stats(values)
    positive_segments = time_segment_positive(trades)
    early_checks = {
        "trades": int(result_stats.get("closed_trades") or 0) >= 10,
        "average_net_bps": finite(result_stats.get("average_net_bps")) and float(result_stats["average_net_bps"]) > 0,
        "profit_factor": finite(result_stats.get("profit_factor")) and float(result_stats["profit_factor"]) > 1.0,
    }
    champion_checks = {
        "trades": int(result_stats.get("closed_trades") or 0) >= 50,
        "average_net_bps": finite(result_stats.get("average_net_bps")) and float(result_stats["average_net_bps"]) >= 3.0,
        "profit_factor": finite(result_stats.get("profit_factor")) and float(result_stats["profit_factor"]) >= 1.30,
        "drawdown": finite(result_stats.get("max_drawdown_bps")) and float(result_stats["max_drawdown_bps"]) <= 100.0,
        "time_segments": positive_segments >= 3,
    }
    return {
        **lane,
        "signal_count": signal_count,
        "stats": result_stats,
        "positive_time_segments": positive_segments,
        "early_checks": early_checks,
        "promising_early": all(early_checks.values()),
        "champion_checks": champion_checks,
        "champion_passed": all(champion_checks.values()),
        "recent_trades": trades[-20:],
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    safety = assert_safe_startup()
    root = Path(args.state_dir)
    snapshots = load_snapshots(root / "MICROSTRUCTURE_SNAPSHOTS.jsonl")
    freeze_ms = parse_time(FREEZE_UTC)
    lanes = [evaluate_lane(lane, snapshots, freeze_ms) for lane in LANES]
    champions = [lane["lane_id"] for lane in lanes if lane["champion_passed"]]
    promising = [lane["lane_id"] for lane in lanes if lane["promising_early"]]
    report = {
        "schema": SCHEMA,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "freeze_utc": FREEZE_UTC,
        "snapshot_count_total": len(snapshots),
        "snapshot_count_after_freeze": sum(1 for row in snapshots if int(row["_ts_ms"]) >= freeze_ms),
        "execution_model": "signal snapshot -> next-snapshot ask entry -> future-snapshot bid exit; observed spread plus conservative TradingCore fees/slippage",
        "lane_count": len(lanes),
        "promising_early": promising,
        "champions": champions,
        "lanes": lanes,
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "live_permission": False,
        "note": "Single-venue forward execution evidence. Even a champion pass requires independent Binance microstructure confirmation before micro-live review.",
    }
    atomic_json(root / "MICROSTRUCTURE_FORWARD_STATUS.json", report)
    print("MICRO_FORWARD", "after_freeze=", report["snapshot_count_after_freeze"], "promising=", promising, "champions=", champions, "LIVE=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
