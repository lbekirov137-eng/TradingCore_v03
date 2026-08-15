#!/usr/bin/env python3
"""Frozen protocol for TradingCore Historical Accelerator V1.

The protocol is intentionally finite: three predeclared long-only hypotheses,
fixed trade geometry, fixed data horizon, and fixed validation gates. No CLI or
environment variable can tune research thresholds after outcomes are observed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = "HISTORICAL_ACCELERATOR_V1"
DATA_SOURCE = "BYBIT_PUBLIC_V5"
HISTORY_DAYS = 730
BAR_INTERVAL = "60"  # 1 hour
HOLDOUT_FRACTION = 0.30

REFERENCE_CAPITAL_USD = 1_000.0
RISK_AMOUNT_USD = 1.0  # 0.1% of reference capital
MAX_LEVERAGE = 1.0
ATR_PERIOD = 14
ATR_STOP_MULTIPLE = 1.50
RISK_REWARD = 2.00
MAX_HOLD_HOURS = 24

# Rolling normalization is backward-looking only.
FUNDING_Z_LOOKBACK = 90
MIN_FUNDING_HISTORY = 40

# Extra cross-sectional evidence beyond generic TradingCore promotion gates.
MIN_EVALUABLE_SYMBOLS_OOS = 5
MIN_OOS_TRADES_PER_SYMBOL = 3
MIN_PROFITABLE_SYMBOL_RATIO = 0.60

FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "id": "FUNDING_CAPITULATION_REBOUND_V1",
        "description": "Extreme negative funding + 8h price drawdown + 8h OI flush; long next 1h open.",
        "funding_z_lte": -1.75,
        "return_8h_lte": -0.030,
        "oi_change_8h_lte": -0.030,
        "require_bullish_signal_candle": False,
    },
    {
        "id": "OI_FLUSH_BULLISH_REVERSAL_V1",
        "description": "4h price selloff + 4h OI flush followed by a fully closed bullish 1h candle.",
        "return_4h_lte": -0.035,
        "oi_change_4h_lte": -0.050,
        "require_bullish_signal_candle": True,
    },
    {
        "id": "SHORT_CROWDING_SQUEEZE_V1",
        "description": "Short-heavy account ratio + non-positive funding + bullish recovery after 4h weakness.",
        "buy_ratio_lte": 0.42,
        "funding_lte": 0.0,
        "return_4h_lte": -0.015,
        "require_bullish_signal_candle": True,
    },
)


def protocol_dict() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "data_source": DATA_SOURCE,
        "history_days": HISTORY_DAYS,
        "bar_interval": BAR_INTERVAL,
        "holdout_fraction": HOLDOUT_FRACTION,
        "reference_capital_usd": REFERENCE_CAPITAL_USD,
        "risk_amount_usd": RISK_AMOUNT_USD,
        "max_leverage": MAX_LEVERAGE,
        "atr_period": ATR_PERIOD,
        "atr_stop_multiple": ATR_STOP_MULTIPLE,
        "risk_reward": RISK_REWARD,
        "max_hold_hours": MAX_HOLD_HOURS,
        "funding_z_lookback": FUNDING_Z_LOOKBACK,
        "min_funding_history": MIN_FUNDING_HISTORY,
        "min_evaluable_symbols_oos": MIN_EVALUABLE_SYMBOLS_OOS,
        "min_oos_trades_per_symbol": MIN_OOS_TRADES_PER_SYMBOL,
        "min_profitable_symbol_ratio": MIN_PROFITABLE_SYMBOL_RATIO,
        "families": list(FAMILIES),
        "execution": {
            "side": "SPOT_LONG_ONLY",
            "entry": "next fully formed 1h bar open after signal bar",
            "stop": "entry - 1.5 * backward-looking ATR14",
            "target": "entry + 2R",
            "same_bar_ordering": "STOP_FIRST",
            "time_stop": "24h close",
            "costs": "TradingCore conservative taker fees + slippage",
        },
        "safety": {
            "paper_research_only": True,
            "private_api": False,
            "real_orders": False,
            "live_permission": False,
            "max_leverage": 1.0,
            "no_averaging": True,
            "collector_a_modified": False,
            "collector_b_modified": False,
            "collector_c_modified": False,
        },
    }


def fingerprint() -> str:
    raw = json.dumps(protocol_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PROTOCOL_FINGERPRINT = fingerprint()

if __name__ == "__main__":
    print(json.dumps({**protocol_dict(), "fingerprint": PROTOCOL_FINGERPRINT}, indent=2))
