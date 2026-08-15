#!/usr/bin/env python3
from __future__ import annotations
import ast
import importlib
from pathlib import Path
from config.startup_safety import assert_safe_startup
from btc_1h_forward_shadow import FORWARD_CLOSED_TARGET, HISTORICAL_UNTOUCHED_HOLDOUT_TRADES

TARGET="cloud_btc_1h_paper_once.py"
FORBIDDEN_IMPORT_ROOTS={"ccxt","binance.client","pybit","okx.Trade","requests"}
FORBIDDEN_CALL_TOKENS=("create_order","place_order","submit_order","cancel_order")

def main()->int:
    safety=assert_safe_startup();root=Path(__file__).resolve().parent;problems=[]
    path=root/TARGET
    if not path.exists():problems.append("MISSING_CLOUD_RUNNER")
    else:
        text=path.read_text(encoding="utf-8")
        try:tree=ast.parse(text,filename=TARGET)
        except SyntaxError as e:
            problems.append(f"SYNTAX:{e}");tree=None
        if tree is not None:
            imports=set()
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):imports.update(a.name for a in node.names)
                elif isinstance(node,ast.ImportFrom) and node.module:imports.add(node.module)
            for module in imports:
                if any(module==x or module.startswith(x+".") for x in FORBIDDEN_IMPORT_ROOTS):
                    problems.append(f"FORBIDDEN_IMPORT:{module}")
        lower=text.lower()
        for token in FORBIDDEN_CALL_TOKENS:
            if token in lower:problems.append(f"FORBIDDEN_CALL_TOKEN:{token}")
    if FORWARD_CLOSED_TARGET!=7:problems.append(f"FORWARD_TARGET:{FORWARD_CLOSED_TARGET}")
    if HISTORICAL_UNTOUCHED_HOLDOUT_TRADES!=23:problems.append(f"HOLDOUT_TARGET:{HISTORICAL_UNTOUCHED_HOLDOUT_TRADES}")
    try:importlib.import_module("cloud_btc_1h_paper_once")
    except Exception as e:problems.append(f"IMPORT_SMOKE:{type(e).__name__}:{e}")
    print("="*84)
    print("TRADINGCORE GITHUB CLOUD PAPER SELFTEST")
    print("Safety:",safety)
    print("Frozen holdout:",HISTORICAL_UNTOUCHED_HOLDOUT_TRADES)
    print("First forward target:",FORWARD_CLOSED_TARGET)
    print("Problems:",problems or "NONE")
    print("REAL ORDER PATH: NOT PRESENT")
    print("="*84)
    return 1 if problems else 0

if __name__=="__main__":raise SystemExit(main())
