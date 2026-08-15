#!/usr/bin/env python3
from __future__ import annotations
import importlib, inspect
from config.startup_safety import assert_safe_startup
import historical_accelerator_v2_protocol as p

PROBLEMS=[]
s=assert_safe_startup()
if s.get("live_trading") is not False: PROBLEMS.append("LIVE_NOT_FALSE")
if s.get("leverage") != 1: PROBLEMS.append("LEVERAGE_NOT_1X")
if len(p.FAMILIES)!=3: PROBLEMS.append("FAMILY_COUNT")
if not (0<p.FINAL_HOLDOUT_FRACTION<0.5): PROBLEMS.append("FINAL_HOLDOUT")
if p.MAX_LEVERAGE!=1.0 or p.RISK_AMOUNT_USD!=1.0: PROBLEMS.append("RISK_CONTRACT")
if p.FINAL_MIN_PF < 1.25 or p.FINAL_MIN_TRADES < 30: PROBLEMS.append("FINAL_GATES_TOO_WEAK")
for modname in ("historical_accelerator_v2_protocol","historical_accelerator_v2"):
    mod=importlib.import_module(modname);src=inspect.getsource(mod).lower()
    forbidden=("place_order(","create_order(","submit_order(","api_secret","bybit_api_key")
    for token in forbidden:
        if token in src: PROBLEMS.append(f"FORBIDDEN:{modname}:{token}")
print("="*88)
print("TRADINGCORE HISTORICAL ACCELERATOR V2 SELFTEST")
print("Safety:",s)
print("Protocol:",p.PROTOCOL_VERSION,p.PROTOCOL_FINGERPRINT)
print("Families:",", ".join(x["id"] for x in p.FAMILIES))
print("Problems:","NONE" if not PROBLEMS else ", ".join(PROBLEMS))
print("REAL ORDER PATH: NOT PRESENT" if not PROBLEMS else "SELFTEST FAILED")
print("="*88)
raise SystemExit(1 if PROBLEMS else 0)
