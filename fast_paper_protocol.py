#!/usr/bin/env python3
"""Frozen protocol for TradingCore Fast PAPER Lab V1.

Experimental PAPER-only parallel lanes intended to accumulate forward evidence
faster than the rare BTC 1H champion. No lane can place real orders or change
the Stable PAPER champion.

The full lane set is frozen before forward outcomes are observed:
6 liquid spot symbols x 2 timeframes x 3 fixed mean-reversion hypotheses.
Each lane is evaluated independently. Cross-lane trade counts/PnL are never
used as evidence that any individual strategy is profitable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = "FAST_PAPER_LAB_V1"
FORWARD_FREEZE_UTC = "2026-08-15T11:15:00+00:00"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "LTCUSDT", "SOLUSDT", "BCHUSDT")
TIMEFRAMES: dict[str, dict[str, int]] = {
    "15m": {"interval_ms": 900_000, "max_bars_in_trade": 96},
    "30m": {"interval_ms": 1_800_000, "max_bars_in_trade": 48},
}
HYPOTHESES = (
    "MR_VWAP_OVERSHOOT_RECLAIM",
    "MR_ATR_FLUSH_REVERSAL",
    "MR_EMA20_BAND_REENTRY",
)
WARMUP_BARS = 60
REFERENCE_CAPITAL_USD = 1_000.0
RISK_AMOUNT_USD = 1.0
MAX_LEVERAGE = 1.0

# Forward-only experimental evidence gate. A pass means PAPER_PROMISING only.
MIN_FORWARD_CLOSED_TRADES = 30
MIN_PROFIT_FACTOR = 1.15
MIN_EXPECTANCY_R = 0.0
MAX_DRAWDOWN_R = 10.0
MIN_SEGMENT_ROBUSTNESS = 0.50
MIN_TRADING_DAYS = 3


def protocol_dict() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "forward_freeze_utc": FORWARD_FREEZE_UTC,
        "symbols": list(SYMBOLS),
        "timeframes": TIMEFRAMES,
        "hypotheses": list(HYPOTHESES),
        "warmup_bars": WARMUP_BARS,
        "reference_capital_usd": REFERENCE_CAPITAL_USD,
        "risk_amount_usd": RISK_AMOUNT_USD,
        "max_leverage": MAX_LEVERAGE,
        "paper_promising_gate": {
            "min_forward_closed_trades": MIN_FORWARD_CLOSED_TRADES,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "min_expectancy_r": MIN_EXPECTANCY_R,
            "max_drawdown_r": MAX_DRAWDOWN_R,
            "min_segment_robustness": MIN_SEGMENT_ROBUSTNESS,
            "min_trading_days": MIN_TRADING_DAYS,
        },
        "safety": {
            "mode": "PAPER_ONLY",
            "spot_long_only": True,
            "max_leverage": 1.0,
            "no_averaging": True,
            "private_exchange_api": False,
            "real_orders": False,
            "live_permission": False,
            "stable_btc_1h_champion_modified": False,
        },
    }


def fingerprint() -> str:
    raw = json.dumps(protocol_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PROTOCOL_FINGERPRINT = fingerprint()
