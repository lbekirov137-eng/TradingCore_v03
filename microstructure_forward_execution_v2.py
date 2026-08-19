#!/usr/bin/env python3
"""TradingCore Microstructure Forward Execution V2.

Adds dual-venue confirmation to the frozen forward execution engine. A signal is
eligible only when Binance and OKX independently agree on the relevant depth or
trade-flow state. Execution remains delayed to the next OKX snapshot at ask and
exits at a future OKX bid with TradingCore's conservative fee/slippage model.

Research/PAPER only. No credentials, balances, transfers or order path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import microstructure_forward_execution as base

base.SCHEMA = "TRADINGCORE_MICROSTRUCTURE_FORWARD_EXECUTION_V2_DUAL_VENUE"
base.LANES = (
    {
        "lane_id": "BTC:CROSS_DEPTH_HIGH_CONTINUATION:H6",
        "symbol": "BTC",
        "signal": "CROSS_DEPTH_HIGH_CONTINUATION",
        "horizon": 6,
    },
    {
        "lane_id": "ETH:CROSS_DEPTH_HIGH_CONTINUATION:H6",
        "symbol": "ETH",
        "signal": "CROSS_DEPTH_HIGH_CONTINUATION",
        "horizon": 6,
    },
    {
        "lane_id": "BTC:CROSS_DEPTH_LOW_REVERSION_LONG:H6",
        "symbol": "BTC",
        "signal": "CROSS_DEPTH_LOW_REVERSION_LONG",
        "horizon": 6,
    },
    {
        "lane_id": "ETH:CROSS_FLOW_SELL_REVERSION_LONG:H3",
        "symbol": "ETH",
        "signal": "CROSS_FLOW_SELL_REVERSION_LONG",
        "horizon": 3,
    },
    {
        "lane_id": "BTC:CROSS_FLOW_SELL_REVERSION_LONG:H3",
        "symbol": "BTC",
        "signal": "CROSS_FLOW_SELL_REVERSION_LONG",
        "horizon": 3,
    },
    {
        "lane_id": "SOL:CROSS_FLOW_BUY_CONTINUATION:H3",
        "symbol": "SOL",
        "signal": "CROSS_FLOW_BUY_CONTINUATION",
        "horizon": 3,
    },
    {
        "lane_id": "ETH:CROSS_DEPTH_AND_FLOW_BUY:H6",
        "symbol": "ETH",
        "signal": "CROSS_DEPTH_AND_FLOW_BUY",
        "horizon": 6,
    },
)


def signal_matches(name: str, row: dict[str, Any]) -> bool:
    flags = row.get("flags") or {}
    if name == "CROSS_DEPTH_HIGH_CONTINUATION":
        return bool(flags.get("cross_venue_depth_high_agreement"))
    if name == "CROSS_DEPTH_LOW_REVERSION_LONG":
        return bool(flags.get("cross_venue_depth_low_agreement"))
    if name == "CROSS_FLOW_SELL_REVERSION_LONG":
        return bool(flags.get("cross_venue_flow_sell_agreement"))
    if name == "CROSS_FLOW_BUY_CONTINUATION":
        return bool(flags.get("cross_venue_flow_buy_agreement"))
    if name == "CROSS_DEPTH_AND_FLOW_BUY":
        return bool(
            flags.get("cross_venue_depth_high_agreement")
            and flags.get("cross_venue_flow_buy_agreement")
        )
    return False


base.signal_matches = signal_matches


def state_dir_from_argv() -> Path | None:
    try:
        index = sys.argv.index("--state-dir")
        return Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return None


def main() -> int:
    state_dir = state_dir_from_argv()
    result = base.main()
    if state_dir is not None:
        report_path = state_dir / "MICROSTRUCTURE_FORWARD_STATUS.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["schema"] = base.SCHEMA
            report["confirmation_model"] = (
                "Signal requires Binance+OKX agreement; next OKX snapshot ask entry; "
                "future OKX bid exit; observed spread plus conservative fees/slippage."
            )
            report["note"] = (
                "Dual-venue forward execution evidence. A champion pass still requires "
                "enough independent trades, time-segment robustness and explicit owner "
                "approval before any micro-live review."
            )
            tmp = report_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(report_path)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
