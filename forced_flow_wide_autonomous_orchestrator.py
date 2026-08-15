#!/usr/bin/env python3
"""Autonomous Collector C -> preregistered Wide V2 research state machine.

No LIVE path. A final historical decision is locked on first eligible evidence
epoch and never re-opened for the same protocol+universe.
"""
from __future__ import annotations
import argparse,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from config.startup_safety import assert_safe_startup
import forced_flow_wide_protocol as protocol

SCHEMA="TRADINGCORE_FORCED_FLOW_WIDE_AUTONOMOUS_V2"
def now()->str:return datetime.now(timezone.utc).isoformat()
def atomic(path:Path,payload:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+".tmp");tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False,default=str),encoding="utf-8");tmp.replace(path)
def read(path:Path)->dict[str,Any]|None:
    if not path.exists():return None
    try:p=json.loads(path.read_text(encoding="utf-8-sig"));return p if isinstance(p,dict) else None
    except Exception:return None
def run(py:str,script:Path,args:list[str],log:Path)->int:
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open("a",encoding="utf-8") as h:
        h.write(f"\n=== {now()} {script.name} ===\n");c=subprocess.run([py,str(script),*args],cwd=str(script.parent),stdout=h,stderr=subprocess.STDOUT,check=False,env={**os.environ,"TRADING_ENVIRONMENT":"PAPER","LIVE_TRADING":"false","PAPER_TRADING":"true","DEMO_ONLY":"true"})
    return int(c.returncode)

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--data-dir",default="C:/TradingCore_Collector_C/data");ap.add_argument("--state-dir",default="C:/TradingCore_Wide_Autonomous");ap.add_argument("--python",default=sys.executable);ap.add_argument("--interval-seconds",type=int,default=900);args=ap.parse_args();safety=assert_safe_startup();root=Path(__file__).resolve().parent;data=Path(args.data_dir);state=Path(args.state_dir);logs=state/"logs";status=state/"status.json";lock_path=state/"historical_decision_lock.json";auth_path=state/"FORWARD_PAPER_AUTHORIZED_BY_RESEARCH.json";state.mkdir(parents=True,exist_ok=True)
    while True:
        universe=read(data/"UNIVERSE_LOCK.json")
        base={"schema":SCHEMA,"updated_at_utc":now(),"safety":safety,"protocol_version":protocol.PROTOCOL_VERSION,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":(universe or {}).get("fingerprint"),"private_api_used":False,"real_orders_enabled":False,"real_order_sent":False,"live_permission":False,"collector_a_modified":False,"collector_b_modified":False}
        if universe is None:
            base["state"]="WAITING_FOR_COLLECTOR_C_UNIVERSE";atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        locked=read(lock_path)
        if locked:
            base["state"]="HISTORICAL_PASS_FORWARD_PAPER" if locked.get("decision")=="HISTORICAL_PROMOTION_PASS" else "WIDE_V2_REJECTED_FROZEN";base["historical_decision_lock"]=locked;base["forward_paper_authorized"]=auth_path.exists();atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        audit_rc=run(args.python,root/"collector_c_g2_g3_audit.py",["--data-dir",str(data)],logs/"g2_g3.log");audit=read(root/"collector_c_audit_results"/"LATEST_COLLECTOR_C_G2_G3.json")
        if audit_rc!=0 or not audit:base["state"]="AUDIT_FAILED_SAFE";base["audit_returncode"]=audit_rc;atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        g2=str((audit.get("g2") or {}).get("state"));g3=str((audit.get("g3") or {}).get("state"));base.update(g2=g2,g3=g3,current_epoch=audit.get("current_epoch"),current_epoch_events=(audit.get("evidence") or {}).get("valid_unique_events"))
        if g2=="G2_REPAIR_REQUIRED" or g3=="G3_REPAIR_REQUIRED":base["state"]="DATA_INTEGRITY_REPAIR_REQUIRED";atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        research_rc=run(args.python,root/"forced_flow_wide_research_portfolio_safe.py",["--data-dir",str(data)],logs/"research.log");research=read(root/"forced_flow_wide_research_results"/"LATEST_FORCED_FLOW_WIDE_RESEARCH.json")
        if research_rc!=0 or not research:base["state"]="RESEARCH_FAILED_SAFE";base["research_returncode"]=research_rc;atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        rs=str(research.get("state") or "UNKNOWN");base["research_state"]=rs;base["selected_epoch"]=research.get("selected_epoch");base["readiness_missing"]=research.get("readiness_missing")
        if rs in ("WAITING_FOR_PREREGISTERED_EPOCH","INSUFFICIENT_TRADE_GEOMETRY"):
            base["state"]="WAITING_FOR_WIDE_PREREGISTERED_SAMPLE";atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        if rs not in ("HISTORICAL_PROMOTION_PASS","HISTORICAL_REJECT_FROZEN"):
            base["state"]="RESEARCH_STATE_UNKNOWN_FAIL_SAFE";atomic(status,base);time.sleep(max(300,args.interval_seconds));continue
        lock={"schema":"TRADINGCORE_FORCED_FLOW_WIDE_DECISION_LOCK_V2","locked_at_utc":now(),"protocol_version":protocol.PROTOCOL_VERSION,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":universe.get("fingerprint"),"decision":rs,"sample_id":research.get("sample_id"),"selected_epoch":research.get("selected_epoch"),"validation":research.get("validation"),"promotion_gates":research.get("promotion_gates"),"real_orders_enabled":False,"live_permission":False};atomic(lock_path,lock)
        if rs=="HISTORICAL_PROMOTION_PASS":
            auth={"schema":"TRADINGCORE_WIDE_FORWARD_PAPER_AUTH_V2","authorized_at_utc":now(),"protocol_version":protocol.PROTOCOL_VERSION,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":universe.get("fingerprint"),"sample_id":research.get("sample_id"),"mode":"PAPER_ONLY","required_forward_closed_trades":protocol.FORWARD_PAPER_MIN_CLOSED_TRADES,"real_orders_enabled":False,"live_permission":False};atomic(auth_path,auth);base["state"]="HISTORICAL_PASS_FORWARD_PAPER_AUTHORIZED";base["forward_paper_authorized"]=True
        else:base["state"]="WIDE_V2_REJECTED_FROZEN";base["forward_paper_authorized"]=False
        atomic(status,base);time.sleep(max(300,args.interval_seconds))
if __name__=="__main__":raise SystemExit(main())
