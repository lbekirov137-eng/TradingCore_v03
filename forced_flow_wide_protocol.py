#!/usr/bin/env python3
"""Immutable preregistered protocol for the wide forced-flow V2 cohort."""
from __future__ import annotations
import hashlib, json
from typing import Any

PROTOCOL_VERSION="FORCED_FLOW_WIDE_REBOUND_V2"
STRATEGY_ID="FORCED_FLOW_WIDE_SPOT_REBOUND_V2"
COLLECTOR_SCHEMA="TRADINGCORE_COLLECTOR_C_BYBIT_WIDE_V1"
COLLECTOR_COHORT="BYBIT_WIDE_ALL_LIQUIDATION_LINEAR_PUBLIC_V1"
LIQUIDATED_SIDE="LONG"
CLUSTER_WINDOW_SECONDS=60
# Cluster notional as basis points of the symbol's 24h perp turnover snapshot
# frozen in UNIVERSE_LOCK at collector start.
THRESHOLD_COHORTS_TURNOVER_BPS=(0.25,0.50,1.00)
PRIMARY_THRESHOLD_TURNOVER_BPS=0.50
HOLDOUT_FRACTION=0.30
PRICE_CATEGORY="spot"
PRICE_INTERVAL="1"
ATR_PERIOD=14
STABILISATION_CANDLES=1
ATR_STOP_MULTIPLE=1.50
RISK_REWARD=2.00
MAX_HOLD_MINUTES=30
RISK_AMOUNT_USD=1.0
REFERENCE_CAPITAL_USD=1000.0
MAX_LEVERAGE=1.0
MIN_VALID_EVENTS_FOR_RESEARCH=1500
MIN_OBSERVATION_SPAN_HOURS=72.0
MIN_PRIMARY_CLUSTERS_FOR_RESEARCH=300
MIN_PRIMARY_SYMBOLS_REPRESENTED=10
MIN_PROFITABLE_THRESHOLD_COHORTS=2
MIN_PROFITABLE_SYMBOL_RATIO=0.60
FORWARD_PAPER_MIN_CLOSED_TRADES=30


def protocol_dict()->dict[str,Any]:
    return {
        "protocol_version":PROTOCOL_VERSION,"strategy_id":STRATEGY_ID,
        "collector_cohort":COLLECTOR_COHORT,"liquidated_side":LIQUIDATED_SIDE,
        "cluster_window_seconds":CLUSTER_WINDOW_SECONDS,
        "threshold_cohorts_turnover_bps":list(THRESHOLD_COHORTS_TURNOVER_BPS),
        "primary_threshold_turnover_bps":PRIMARY_THRESHOLD_TURNOVER_BPS,
        "holdout_fraction":HOLDOUT_FRACTION,"price_category":PRICE_CATEGORY,"price_interval":PRICE_INTERVAL,
        "atr_period":ATR_PERIOD,"stabilisation_candles":STABILISATION_CANDLES,
        "atr_stop_multiple":ATR_STOP_MULTIPLE,"risk_reward":RISK_REWARD,"max_hold_minutes":MAX_HOLD_MINUTES,
        "risk_amount_usd":RISK_AMOUNT_USD,"reference_capital_usd":REFERENCE_CAPITAL_USD,"max_leverage":MAX_LEVERAGE,
        "min_valid_events_for_research":MIN_VALID_EVENTS_FOR_RESEARCH,
        "min_observation_span_hours":MIN_OBSERVATION_SPAN_HOURS,
        "min_primary_clusters_for_research":MIN_PRIMARY_CLUSTERS_FOR_RESEARCH,
        "min_primary_symbols_represented":MIN_PRIMARY_SYMBOLS_REPRESENTED,
        "min_profitable_threshold_cohorts":MIN_PROFITABLE_THRESHOLD_COHORTS,
        "min_profitable_symbol_ratio":MIN_PROFITABLE_SYMBOL_RATIO,
        "forward_paper_min_closed_trades":FORWARD_PAPER_MIN_CLOSED_TRADES,
        "entry_rule":("Aggregate same-symbol LONG-liquidations in fixed 60s chains. Divide cluster USDT quote-notional by the symbol's frozen 24h turnover snapshot and convert to bps. If >= frozen threshold, wait for exactly one fully closed bullish 1m SPOT candle after cluster end, then enter PAPER LONG at next spot 1m open."),
        "exit_rule":("Stop = entry - 1.5*pre-event spot ATR14; target = entry + 2R; conservative stop-first same-candle ordering; time-stop at 30m close."),
        "universe_rule":("Universe comes only from Collector C UNIVERSE_LOCK: top-turnover Bybit Trading USDT LinearPerpetual symbols that also have active USDT spot markets, frozen before outcome research."),
        "safety":{"spot_long_only":True,"max_leverage":1.0,"no_averaging":True,"private_api":False,"real_orders":False,"collector_a_modified":False,"collector_b_modified":False}
    }

def fingerprint()->str:
    raw=json.dumps(protocol_dict(),sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode()).hexdigest()
PROTOCOL_FINGERPRINT=fingerprint()

if __name__=="__main__": print(json.dumps({**protocol_dict(),"fingerprint":PROTOCOL_FINGERPRINT},indent=2))
