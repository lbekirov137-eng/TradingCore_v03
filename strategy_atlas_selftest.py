#!/usr/bin/env python3
from __future__ import annotations
import ast, importlib
from pathlib import Path
from config.startup_safety import assert_safe_startup
FILES=("strategy_atlas_protocol.py","strategy_atlas_price_v2.py")
FORBIDDEN=("create_order","place_order","submit_order","api_key","secret_key","live_permission=true")
def main():
 safe=assert_safe_startup();root=Path(__file__).resolve().parent;problems=[]
 for name in FILES:
  p=root/name
  if not p.exists():problems.append(f"MISSING:{name}");continue
  text=p.read_text(encoding="utf-8")
  try:ast.parse(text,filename=name)
  except SyntaxError as e:problems.append(f"SYNTAX:{name}:{e}")
  low=text.lower()
  for token in FORBIDDEN:
   if token in low:problems.append(f"FORBIDDEN:{name}:{token}")
 try:
  proto=importlib.import_module("strategy_atlas_protocol")
  importlib.import_module("strategy_atlas_price_v2")
  if len(proto.PRICE_FAMILIES)<10:problems.append("PRICE_FAMILY_COVERAGE_TOO_SMALL")
  if len(proto.SPOT_SYMBOLS)<6:problems.append("SYMBOL_COVERAGE_TOO_SMALL")
 except Exception as e:problems.append(f"IMPORT:{type(e).__name__}:{e}")
 print("="*88);print("TRADINGCORE STRATEGY ATLAS SELFTEST");print("Safety:",safe);print("Problems:",problems or "NONE");print("REAL ORDER PATH: NOT PRESENT");print("="*88)
 return 1 if problems else 0
if __name__=="__main__":raise SystemExit(main())
