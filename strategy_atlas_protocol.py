#!/usr/bin/env python3
"""Frozen parallel research protocol for TradingCore Strategy Atlas V1.

Goal: cover structurally different trading mechanisms in parallel instead of
serially inventing one more EMA/VWAP variant. Research/PAPER only. No private
API, no authenticated account data, no order placement, no LIVE permission.

This is intentionally broad but finite. "Every conceivable strategy" is not a
well-defined testable set, so the atlas freezes a representative mechanism map
covering directional, mean-reversion, relative-value, carry, flow, volatility,
session/calendar and microstructure/arbitrage families.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = "STRATEGY_ATLAS_V1_PARALLEL_MECHANISM_MAP"

SPOT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "LTCUSDT", "BCHUSDT",
)
SPOT_TIMEFRAMES = ("1h", "4h")
SPOT_HISTORY_DAYS = 365
SPOT_VENUES = ("BINANCE", "OKX")

PRICE_FAMILIES = (
    "TREND_DONCHIAN_BREAKOUT",
    "TREND_EMA_PULLBACK",
    "TIME_SERIES_MOMENTUM",
    "VOLUME_CONFIRMED_BREAKOUT",
    "VOL_COMPRESSION_BREAKOUT",
    "RSI_PANIC_MEAN_REVERSION",
    "BOLLINGER_REENTRY_MEAN_REVERSION",
    "RANGE_FADE",
    "OPENING_RANGE_BREAKOUT_UTC",
    "SESSION_MOMENTUM_ASIA_EU_US",
    "WEEKEND_WEEKDAY_ROTATION",
    "VOLATILITY_REGIME_SWITCH",
)

RELATIVE_VALUE_FAMILIES = (
    "CROSS_SECTIONAL_MOMENTUM_ROTATION",
    "CROSS_SECTIONAL_SHORT_TERM_REVERSAL",
    "BTC_ETH_RATIO_MEAN_REVERSION",
    "SOL_ETH_RATIO_MEAN_REVERSION",
    "ALT_RESIDUAL_REVERSAL_VS_BTC",
    "BETA_NEUTRAL_RELATIVE_MOMENTUM",
)

DERIVATIVE_FAMILIES = (
    "POSITIVE_FUNDING_CARRY",
    "NEGATIVE_FUNDING_REVERSE_CARRY",
    "FUNDING_EXTREME_REVERSAL",
    "PREMIUM_INDEX_CONVERGENCE",
    "MARK_INDEX_DIVERGENCE",
    "OI_PRICE_DIVERGENCE",
    "CROWDING_SQUEEZE",
    "LIQUIDATION_CASCADE_REBOUND",
)

MICROSTRUCTURE_FAMILIES = (
    "CROSS_EXCHANGE_SPREAD_FEASIBILITY",
    "TRIANGULAR_ARBITRAGE_FEASIBILITY",
    "ORDERBOOK_IMBALANCE_SCOUT",
    "TRADE_FLOW_IMBALANCE_SCOUT",
    "PASSIVE_MARKET_MAKING_SPREAD_SCOUT",
    "SPOT_PERP_BASIS_SNAPSHOT_SCOUT",
)

NICHE_FAMILIES = (
    "GRID_RANGE_CAPTURE",
    "VOLATILITY_BREAKOUT_AFTER_DEAD_ZONE",
    "FAILED_BREAKOUT_FADE",
    "LIQUIDITY_SWEEP_RECLAIM_PROXY",
    "DAY_OF_WEEK_EFFECT",
    "UTC_TIME_BUCKET_EFFECT",
    "CORRELATION_BREAK_REVERSAL",
    "CORRELATION_BREAK_MOMENTUM",
)

# Generic historical screen gates. Passing here is a shortlist, never LIVE.
MIN_TRADES_PER_VENUE = 40
MIN_TRADES_PER_HALF = 15
MIN_FULL_PF = 1.20
MIN_FULL_EXPECTANCY_R = 0.05
MAX_FULL_DD_R = 10.0
MIN_HALF_PF = 1.02
MIN_HALF_EXPECTANCY_R = 0.0
MIN_SEGMENT_ROBUSTNESS = 0.50

REFERENCE_CAPITAL_USD = 1000.0
RISK_AMOUNT_USD = 1.0
MAX_GROSS_LEVERAGE_RESEARCH = 1.0


def protocol_dict() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "spot_symbols": list(SPOT_SYMBOLS),
        "spot_timeframes": list(SPOT_TIMEFRAMES),
        "spot_history_days": SPOT_HISTORY_DAYS,
        "spot_venues": list(SPOT_VENUES),
        "mechanism_map": {
            "price": list(PRICE_FAMILIES),
            "relative_value": list(RELATIVE_VALUE_FAMILIES),
            "derivatives": list(DERIVATIVE_FAMILIES),
            "microstructure": list(MICROSTRUCTURE_FAMILIES),
            "niche": list(NICHE_FAMILIES),
        },
        "historical_gates": {
            "min_trades_per_venue": MIN_TRADES_PER_VENUE,
            "min_trades_per_half": MIN_TRADES_PER_HALF,
            "min_full_pf": MIN_FULL_PF,
            "min_full_expectancy_r": MIN_FULL_EXPECTANCY_R,
            "max_full_dd_r": MAX_FULL_DD_R,
            "min_half_pf": MIN_HALF_PF,
            "min_half_expectancy_r": MIN_HALF_EXPECTANCY_R,
            "min_segment_robustness": MIN_SEGMENT_ROBUSTNESS,
        },
        "risk": {
            "reference_capital_usd": REFERENCE_CAPITAL_USD,
            "risk_amount_usd": RISK_AMOUNT_USD,
            "max_gross_leverage_research": MAX_GROSS_LEVERAGE_RESEARCH,
        },
        "selection_rule": (
            "NO FINAL-DATA TUNING. Historical candidates must survive chronology "
            "and independent venue checks. Flow/microstructure families that lack "
            "sufficient public historical depth are forward-scoped and cannot be "
            "promoted from snapshots alone."
        ),
        "safety": {
            "research_paper_only": True,
            "private_api": False,
            "real_orders": False,
            "live_permission": False,
            "stable_champion_modified": False,
        },
    }


def fingerprint() -> str:
    raw = json.dumps(protocol_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PROTOCOL_FINGERPRINT = fingerprint()
