#!/usr/bin/env python3
"""Frozen independent price-only Historical Accelerator V2 protocol.

This is NOT a retune of Historical Accelerator V1. V1 stays sealed.
Three new families are preregistered before V2 outcomes are inspected.
"""
from __future__ import annotations
import hashlib, json
from typing import Any

PROTOCOL_VERSION = "HISTORICAL_ACCELERATOR_V2_PRICE_ONLY"
HISTORY_DAYS = 730
FINAL_HOLDOUT_FRACTION = 0.20
PRE_FINAL_FRACTION = 0.80
REFERENCE_CAPITAL_USD = 1000.0
RISK_AMOUNT_USD = 1.0
MAX_LEVERAGE = 1.0
ATR_PERIOD = 14

# Multiple-family pre-final selection gates.
PREFINAL_MIN_OOS_TRADES = 35
PREFINAL_MIN_PF = 1.20
PREFINAL_MIN_EXPECTANCY_R = 0.05
PREFINAL_MAX_DD_R = 10.0
PREFINAL_MIN_ROBUSTNESS = 0.60

# Sealed final confirmation gates. Stricter than the generic TradingCore gates.
FINAL_MIN_TRADES = 30
FINAL_MIN_PF = 1.25
FINAL_MIN_EXPECTANCY_R = 0.08
FINAL_MAX_DD_R = 8.0
FINAL_MIN_REPRESENTED_SYMBOLS = 5
FINAL_MIN_EVALUABLE_SYMBOLS = 3
FINAL_MIN_TRADES_PER_SYMBOL = 3
FINAL_MIN_PROFITABLE_SYMBOL_RATIO = 0.60

# Family order is part of the protocol. If more than one passes pre-final,
# the FIRST family in this tuple is selected; final holdout is opened only for it.
FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "id": "CROSS_SECTIONAL_MOMENTUM_V2",
        "evaluation": "UTC daily close only",
        "r7_min": 0.05,
        "r30_min": 0.10,
        "ema_hours": 168,
        "rank": "r7 + 0.50*r30",
        "atr_stop_multiple": 2.0,
        "risk_reward": 3.0,
        "max_hold_hours": 72,
    },
    {
        "id": "TREND_PULLBACK_RESUMPTION_V2",
        "evaluation": "every 4h close",
        "r7_min": 0.05,
        "r24_min": -0.06,
        "r24_max": -0.015,
        "ema_hours": 168,
        "require_bullish_signal": True,
        "rank": "r7 - abs(r24)",
        "atr_stop_multiple": 1.75,
        "risk_reward": 2.5,
        "max_hold_hours": 48,
    },
    {
        "id": "VOLATILITY_COMPRESSION_BREAKOUT_V2",
        "evaluation": "hourly close",
        "r7_min": 0.03,
        "breakout_lookback_hours": 24,
        "atr_percentile_lookback_hours": 168,
        "atr_percentile_max": 0.30,
        "turnover_median_lookback_hours": 24,
        "turnover_multiple_min": 1.50,
        "rank": "breakout_distance_in_atr",
        "atr_stop_multiple": 1.50,
        "risk_reward": 3.0,
        "max_hold_hours": 48,
    },
)


def protocol_dict() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "history_days": HISTORY_DAYS,
        "final_holdout_fraction": FINAL_HOLDOUT_FRACTION,
        "pre_final_fraction": PRE_FINAL_FRACTION,
        "reference_capital_usd": REFERENCE_CAPITAL_USD,
        "risk_amount_usd": RISK_AMOUNT_USD,
        "max_leverage": MAX_LEVERAGE,
        "atr_period": ATR_PERIOD,
        "prefinal_gates": {
            "min_oos_trades": PREFINAL_MIN_OOS_TRADES,
            "min_pf": PREFINAL_MIN_PF,
            "min_expectancy_r": PREFINAL_MIN_EXPECTANCY_R,
            "max_dd_r": PREFINAL_MAX_DD_R,
            "min_robustness": PREFINAL_MIN_ROBUSTNESS,
        },
        "final_gates": {
            "min_trades": FINAL_MIN_TRADES,
            "min_pf": FINAL_MIN_PF,
            "min_expectancy_r": FINAL_MIN_EXPECTANCY_R,
            "max_dd_r": FINAL_MAX_DD_R,
            "min_represented_symbols": FINAL_MIN_REPRESENTED_SYMBOLS,
            "min_evaluable_symbols": FINAL_MIN_EVALUABLE_SYMBOLS,
            "min_trades_per_symbol": FINAL_MIN_TRADES_PER_SYMBOL,
            "min_profitable_symbol_ratio": FINAL_MIN_PROFITABLE_SYMBOL_RATIO,
        },
        "families": list(FAMILIES),
        "execution": {
            "side": "SPOT_LONG_ONLY",
            "entry": "next 1h bar open after fully closed signal bar",
            "same_bar_ordering": "STOP_FIRST",
            "costs": "TradingCore conservative taker fees + slippage",
            "portfolio_positions": 1,
            "no_averaging": True,
        },
        "safety": {
            "paper_research_only": True,
            "private_api": False,
            "real_orders": False,
            "live_permission": False,
            "collector_a_modified": False,
            "collector_b_modified": False,
            "collector_c_modified": False,
        },
    }


def fingerprint() -> str:
    raw = json.dumps(protocol_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()

PROTOCOL_FINGERPRINT = fingerprint()

if __name__ == "__main__":
    print(json.dumps({**protocol_dict(), "fingerprint": PROTOCOL_FINGERPRINT}, indent=2))
