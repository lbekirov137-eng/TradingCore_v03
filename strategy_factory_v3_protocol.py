#!/usr/bin/env python3
"""Frozen bounded protocol for TradingCore Strategy Factory V3.

Purpose: find a more frequent SPOT-LONG-only candidate quickly without opening
an endless research loop. Development/selection uses only the EARLY half of the
already-cached Bybit 730-day sample. The final test uses the LATER half of the
same chronology from a DIFFERENT public venue (OKX), so final data is not used
for parameter/family selection.

No LIVE path. No private API. No leverage above 1x. No averaging.
"""
from __future__ import annotations
import hashlib, json
from typing import Any

PROTOCOL_VERSION="STRATEGY_FACTORY_V3_TIME_VENUE_HOLDOUT"
SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT")
DEV_SOURCE="BYBIT_PUBLIC_SPOT_CACHED_EARLY_HALF"
FINAL_SOURCE="OKX_PUBLIC_SPOT_LATE_HALF"
BAR_HOURS=1
REFERENCE_CAPITAL_USD=1000.0
RISK_AMOUNT_USD=1.0
MAX_LEVERAGE=1.0
ATR_PERIOD=14
ATR_STOP_MULTIPLE=1.5
TARGET_R=2.0
MAX_HOLD_HOURS=36
MAX_OPEN_POSITIONS_GLOBAL=1

# The search grid is fixed here BEFORE final OKX history is fetched/evaluated.
# Parameter search is allowed on development only; exactly one winner may open final.
FAMILIES=(
 {"id":"DONCHIAN_TREND_BREAKOUT_V3","fast_ema":[48,72],"slow_ema":[168,240],"breakout_hours":[24,48]},
 {"id":"TREND_PULLBACK_RECOVERY_V3","fast_ema":[48,72],"slow_ema":[168,240],"pullback_atr":[0.5,0.8],"lookback_hours":[12,24]},
 {"id":"PANIC_MEAN_REVERSION_V3","rsi_period":[14],"rsi_lte":[25,30],"drop_24h_lte":[-0.05,-0.07],"trend_filter_ema":[240]},
)

DEV_MIN_TRADES=35
DEV_MIN_PF=1.10
DEV_MIN_EXPECTANCY_R=0.03
DEV_MAX_DD_R=12.0
DEV_MIN_ROBUSTNESS=0.50

FINAL_MIN_TRADES=30
FINAL_MIN_PF=1.20
FINAL_MIN_EXPECTANCY_R=0.05
FINAL_MAX_DD_R=10.0
FINAL_MIN_PROFITABLE_SYMBOLS=2
FINAL_MIN_ROBUSTNESS=0.50

FORWARD_PAPER_MIN_TRADES=30

def protocol_dict()->dict[str,Any]:
 return {
  "protocol_version":PROTOCOL_VERSION,"symbols":list(SYMBOLS),"dev_source":DEV_SOURCE,"final_source":FINAL_SOURCE,
  "bar_hours":BAR_HOURS,"reference_capital_usd":REFERENCE_CAPITAL_USD,"risk_amount_usd":RISK_AMOUNT_USD,
  "max_leverage":MAX_LEVERAGE,"atr_period":ATR_PERIOD,"atr_stop_multiple":ATR_STOP_MULTIPLE,"target_r":TARGET_R,
  "max_hold_hours":MAX_HOLD_HOURS,"max_open_positions_global":MAX_OPEN_POSITIONS_GLOBAL,"families":list(FAMILIES),
  "dev_gates":{"min_trades":DEV_MIN_TRADES,"min_pf":DEV_MIN_PF,"min_expectancy_r":DEV_MIN_EXPECTANCY_R,"max_dd_r":DEV_MAX_DD_R,"min_robustness":DEV_MIN_ROBUSTNESS},
  "final_gates":{"min_trades":FINAL_MIN_TRADES,"min_pf":FINAL_MIN_PF,"min_expectancy_r":FINAL_MIN_EXPECTANCY_R,"max_dd_r":FINAL_MAX_DD_R,"min_profitable_symbols":FINAL_MIN_PROFITABLE_SYMBOLS,"min_robustness":FINAL_MIN_ROBUSTNESS},
  "forward_paper_min_trades":FORWARD_PAPER_MIN_TRADES,
  "safety":{"spot_long_only":True,"private_api":False,"real_orders":False,"live_permission":False,"no_averaging":True,"max_leverage":1.0}
 }

def fingerprint()->str:
 raw=json.dumps(protocol_dict(),sort_keys=True,separators=(",",":"))
 return hashlib.sha256(raw.encode()).hexdigest()
PROTOCOL_FINGERPRINT=fingerprint()
