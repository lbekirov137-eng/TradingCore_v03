#!/usr/bin/env python3
"""Immutable preregistered protocol for TradingCore forced-flow research.

This module contains NO strategy search and NO runtime-tunable research
parameters. A different hypothesis requires a new version/file, not environment
variables or CLI switches.

Primary question
----------------
Do large Bybit linear LONG-liquidation cascades predict a short-horizon rebound
that remains profitable after conservative costs under the existing TradingCore
0.1% risk / 1x spot / long-only safety contract?

Data source is Collector B only. Collector A is never read or modified.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = "FORCED_FLOW_REBOUND_V1"
STRATEGY_ID = "FORCED_FLOW_LONG_LIQ_REBOUND_V1"

VENUE = "BYBIT"
COHORT = "BYBIT_ALL_LIQUIDATION_LINEAR_PUBLIC_V1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
LIQUIDATED_SIDE = "LONG"

# Fixed clustering / size cohorts. No threshold is selected after seeing results.
CLUSTER_WINDOW_SECONDS = 60
THRESHOLD_COHORTS_USDT = (250_000.0, 500_000.0, 1_000_000.0)
PRIMARY_THRESHOLD_USDT = 250_000.0

# Fixed chronology. Holdout is the final 30%; development is split again only
# for reporting/diagnostics. There is no parameter optimisation.
HOLDOUT_FRACTION = 0.30

# Outcome/trade geometry frozen before evidence inspection.
PRICE_INTERVAL = "1"                 # Bybit 1-minute linear kline
ATR_PERIOD = 14
STABILISATION_CANDLES = 1            # exactly first full candle after cascade
ATR_STOP_MULTIPLE = 1.50
RISK_REWARD = 2.00                   # TP distance = 2R
MAX_HOLD_MINUTES = 30
RISK_AMOUNT_USD = 1.0                # 0.1% of $1,000 reference capital
REFERENCE_CAPITAL_USD = 1_000.0
MAX_LEVERAGE = 1.0

# Data-readiness for outcome research. These are stronger than preliminary G3.
# They are evidence-volume gates, not profitability gates. 300 primary clusters
# is intentionally conservative: after the fixed bullish-stabilisation and 1x
# geometry filters it materially reduces the risk of opening the one-time final
# holdout with fewer than the required 30 OOS trades.
MIN_VALID_EVENTS_FOR_RESEARCH = 1_000
MIN_OBSERVATION_SPAN_HOURS = 72.0
MIN_PRIMARY_CLUSTERS_FOR_RESEARCH = 300

# Cross-threshold robustness: same frozen rule must be profitable in at least
# two of the three predeclared notional cohorts before promotion can pass.
MIN_PROFITABLE_THRESHOLD_COHORTS = 2

# Forward PAPER confirmation after historical promotion.
FORWARD_PAPER_MIN_CLOSED_TRADES = 30


def protocol_dict() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "strategy_id": STRATEGY_ID,
        "venue": VENUE,
        "cohort": COHORT,
        "symbols": list(SYMBOLS),
        "liquidated_side": LIQUIDATED_SIDE,
        "cluster_window_seconds": CLUSTER_WINDOW_SECONDS,
        "threshold_cohorts_usdt": list(THRESHOLD_COHORTS_USDT),
        "primary_threshold_usdt": PRIMARY_THRESHOLD_USDT,
        "holdout_fraction": HOLDOUT_FRACTION,
        "price_interval": PRICE_INTERVAL,
        "atr_period": ATR_PERIOD,
        "stabilisation_candles": STABILISATION_CANDLES,
        "atr_stop_multiple": ATR_STOP_MULTIPLE,
        "risk_reward": RISK_REWARD,
        "max_hold_minutes": MAX_HOLD_MINUTES,
        "risk_amount_usd": RISK_AMOUNT_USD,
        "reference_capital_usd": REFERENCE_CAPITAL_USD,
        "max_leverage": MAX_LEVERAGE,
        "min_valid_events_for_research": MIN_VALID_EVENTS_FOR_RESEARCH,
        "min_observation_span_hours": MIN_OBSERVATION_SPAN_HOURS,
        "min_primary_clusters_for_research": MIN_PRIMARY_CLUSTERS_FOR_RESEARCH,
        "min_profitable_threshold_cohorts": MIN_PROFITABLE_THRESHOLD_COHORTS,
        "forward_paper_min_closed_trades": FORWARD_PAPER_MIN_CLOSED_TRADES,
        "entry_rule": (
            "For a same-symbol LONG-liquidation cascade, aggregate executed-size"
            " x bankruptcy-price quote notional over the fixed 60s cluster. If"
            " aggregate >= threshold, wait for exactly the first fully closed 1m"
            " candle after cluster end. Trade only if that candle is bullish;"
            " enter LONG at the next 1m candle open."
        ),
        "exit_rule": (
            "Stop = entry - 1.5*pre-event ATR14; target = entry + 2R; stop wins"
            " if stop and target touch in the same candle; time-stop at 30m close."
        ),
        "safety": {
            "spot_long_only": True,
            "no_leverage_above_1x": True,
            "no_averaging": True,
            "one_open_position_per_symbol": True,
            "private_exchange_api": False,
            "real_orders": False,
            "collector_a_modified": False,
        },
    }


def fingerprint() -> str:
    raw = json.dumps(protocol_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PROTOCOL_FINGERPRINT = fingerprint()

if __name__ == "__main__":
    print(json.dumps({**protocol_dict(), "fingerprint": PROTOCOL_FINGERPRINT}, indent=2))
