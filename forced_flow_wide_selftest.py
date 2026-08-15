#!/usr/bin/env python3
"""Safety/import self-test for the Wide V2 stack."""
from __future__ import annotations
import py_compile
from pathlib import Path
from config.startup_safety import assert_safe_startup
import forced_flow_wide_protocol as protocol

ROOT=Path(__file__).resolve().parent
FILES=[
 "collector_c_bybit_wide.py","collector_c_g2_g3_audit.py",
 "forced_flow_wide_protocol.py","forced_flow_wide_research_engine.py",
 "forced_flow_wide_research_portfolio_safe.py","forced_flow_wide_autonomous_orchestrator.py",
 "forced_flow_wide_forward_paper.py",
]
DANGEROUS=("/v5/order/","create_order(","place_order(","submit_order(","api_secret","secret_key","private_key")

def main()->int:
    safety=assert_safe_startup();problems=[]
    for name in FILES:
        path=ROOT/name
        if not path.exists():problems.append(f"MISSING:{name}");continue
        try:py_compile.compile(str(path),doraise=True)
        except Exception as e:problems.append(f"COMPILE:{name}:{type(e).__name__}:{e}")
        text=path.read_text(encoding="utf-8").lower()
        for token in DANGEROUS:
            if token in text:problems.append(f"DANGEROUS_TOKEN:{name}:{token}")
    if protocol.MAX_CONCURRENT_POSITIONS!=1:problems.append("MAX_CONCURRENT_POSITIONS_NOT_1")
    if protocol.MAX_LEVERAGE!=1.0:problems.append("MAX_LEVERAGE_NOT_1")
    print("="*88);print("TRADINGCORE WIDE FORCED-FLOW V2 SELFTEST");print("Safety:",safety);print("Protocol:",protocol.PROTOCOL_VERSION,protocol.PROTOCOL_FINGERPRINT);print("Problems:",problems or "NONE");print("REAL ORDER PATH:","BLOCKED" if problems else "NOT PRESENT");print("="*88)
    return 1 if problems else 0
if __name__=="__main__":raise SystemExit(main())
