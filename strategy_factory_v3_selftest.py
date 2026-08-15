#!/usr/bin/env python3
from __future__ import annotations
import ast, importlib
from pathlib import Path
from config.startup_safety import assert_safe_startup
import strategy_factory_v3_protocol as p

FILES=("strategy_factory_v3_protocol.py","strategy_factory_v3.py")
FORBIDDEN_IMPORT_ROOTS={"ccxt","requests"}
FORBIDDEN_CALLS={"create_order","place_order","submit_order","cancel_order"}

def main()->int:
 safety=assert_safe_startup();root=Path(__file__).resolve().parent;problems=[]
 if p.MAX_LEVERAGE!=1.0:problems.append("MAX_LEVERAGE_NOT_1X")
 if p.MAX_OPEN_POSITIONS_GLOBAL!=1:problems.append("GLOBAL_POSITION_LIMIT_NOT_1")
 if len(p.FAMILIES)!=3:problems.append("FAMILY_COUNT_CHANGED")
 for name in FILES:
  path=root/name
  if not path.exists():problems.append(f"MISSING:{name}");continue
  try:tree=ast.parse(path.read_text(encoding="utf-8"),filename=name)
  except SyntaxError as e:problems.append(f"SYNTAX:{name}:{e}");continue
  for node in ast.walk(tree):
   if isinstance(node,ast.Import):
    for a in node.names:
     if a.name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS:problems.append(f"FORBIDDEN_IMPORT:{a.name}")
   elif isinstance(node,ast.ImportFrom) and node.module and node.module.split(".")[0] in FORBIDDEN_IMPORT_ROOTS:problems.append(f"FORBIDDEN_IMPORT:{node.module}")
   elif isinstance(node,ast.Call):
    fn=node.func
    if isinstance(fn,ast.Name) and fn.id in FORBIDDEN_CALLS:problems.append(f"FORBIDDEN_CALL:{fn.id}")
    if isinstance(fn,ast.Attribute) and fn.attr in FORBIDDEN_CALLS:problems.append(f"FORBIDDEN_CALL:{fn.attr}")
 try:importlib.import_module("strategy_factory_v3")
 except Exception as e:problems.append(f"IMPORT_SMOKE:{type(e).__name__}:{e}")
 print("="*88);print("TRADINGCORE STRATEGY FACTORY V3 SELFTEST");print("Safety:",safety);print("Protocol:",p.PROTOCOL_VERSION,p.PROTOCOL_FINGERPRINT);print("Families:",", ".join(x["id"] for x in p.FAMILIES));print("Problems:",problems or "NONE");print("REAL ORDER PATH: NOT PRESENT");print("="*88)
 return 1 if problems else 0
if __name__=="__main__":raise SystemExit(main())
