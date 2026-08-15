#!/usr/bin/env python3
"""Frozen rapid historical evidence protocol for the already-frozen Fast PAPER V1 lanes.

This is NOT a new strategy search and performs no parameter tuning. It takes the
36 Fast PAPER V1 lanes exactly as frozen on 2026-08-15 and stress-tests them on
365 days of public closed spot candles from two venues: Binance and Bybit.

A lane is RAPID_EVIDENCE_PASS only when it is positive after TradingCore costs
on BOTH venues and on BOTH chronological halves of each venue. This is a fast
robustness screen, not LIVE authorization and not a guarantee of future profit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = "RAPID_REPLAY_V1_TWO_VENUE_FOUR_SEGMENT"
HISTORY_DAYS = 365
VENUES = ("BINANCE", "BYBIT")

# Frozen Fast PAPER lane set. Importing these values changes no strategy rules.
from fast_paper_protocol import HYPOTHESES, SYMBOLS, TIMEFRAMES

MIN_TRADES_PER_VENUE = 50
MIN_TRADES_PER_HALF = 20
MIN_FULL_PF = 1.20
MIN_FULL_EXPECTANCY_R = 0.05
MAX_FULL_DD_R = 10.0
MIN_HALF_PF = 1.05
MIN_HALF_EXPECTANCY_R = 0.0
MIN_SEGMENT_ROBUSTNESS = 0.50


def protocol_dict() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "history_days": HISTORY_DAYS,
        "venues": list(VENUES),
        "symbols": list(SYMBOLS),
        "timeframes": TIMEFRAMES,
        "hypotheses": list(HYPOTHESES),
        "gates": {
            "min_trades_per_venue": MIN_TRADES_PER_VENUE,
            "min_trades_per_half": MIN_TRADES_PER_HALF,
            "min_full_pf": MIN_FULL_PF,
            "min_full_expectancy_r": MIN_FULL_EXPECTANCY_R,
            "max_full_dd_r": MAX_FULL_DD_R,
            "min_half_pf": MIN_HALF_PF,
            "min_half_expectancy_r": MIN_HALF_EXPECTANCY_R,
            "min_segment_robustness": MIN_SEGMENT_ROBUSTNESS,
        },
        "selection_rule": "NO_TUNING; lane must pass Binance full+halves AND Bybit full+halves",
        "safety": {
            "paper_research_only": True,
            "spot_long_only": True,
            "max_leverage": 1.0,
            "private_api": False,
            "real_orders": False,
            "live_permission": False,
            "stable_champion_modified": False,
        },
    }


def fingerprint() -> str:
    raw = json.dumps(protocol_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


PROTOCOL_FINGERPRINT = fingerprint()
