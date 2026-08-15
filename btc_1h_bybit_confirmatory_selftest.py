#!/usr/bin/env python3
from __future__ import annotations
import ast
import importlib
from pathlib import Path
from config.startup_safety import assert_safe_startup

FILES=("btc_1h_bybit_confirmatory.py","btc_1h_forward_shadow.py")
FORBIDDEN=("create_order","place_order","submit_order","api_key","secret_key")
FORBIDDEN_IMPORTS=("strategy_lab_orchestrator","strategy_lab_deep_dive","requests")

def main()->int:
    safety=assert_safe_startup();root=Path(__file__).resolve().parent;problems=[]
    for name in FILES:
        path=root/name
        if not path.exists():problems.append(f"MISSING:{name}");continue
        text=path.read_text(encoding="utf-8")
        try:ast.parse(text,filename=name)
        except SyntaxError as e:problems.append(f"SYNTAX:{name}:{e}")
        lower=text.lower()
        if name=="btc_1h_bybit_confirmatory.py":
            for token in FORBIDDEN:
                if token in lower:problems.append(f"FORBIDDEN_TOKEN:{token}")
            for module in FORBIDDEN_IMPORTS:
                if f"import {module}" in lower or f"from {module}" in lower:
                    problems.append(f"FORBIDDEN_IMPORT:{module}")
    try:
        importlib.import_module("btc_1h_bybit_confirmatory")
    except Exception as e:
        problems.append(f"IMPORT_SMOKE:{type(e).__name__}:{e}")
    print("="*84)
    print("BTC 1H BYBIT CONFIRMATORY SELFTEST V2")
    print("Safety:",safety)
    print("Problems:",problems or "NONE")
    print("Dependency smoke import: PASS" if not any(str(p).startswith("IMPORT_SMOKE:") for p in problems) else "Dependency smoke import: FAIL")
    print("REAL ORDER PATH: NOT PRESENT")
    print("="*84)
    return 1 if problems else 0
if __name__=="__main__":raise SystemExit(main())
