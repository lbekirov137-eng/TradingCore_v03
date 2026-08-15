#!/usr/bin/env python3
"""Forward PAPER confirmation for Historical Accelerator V2.
Inert until V2 creates CANDIDATE_FOR_FORWARD_PAPER.json. No order path.
"""
from __future__ import annotations
import argparse,json,time
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request,urlopen

from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs
from api.strategy_supervisor.stats import ClosedTrade,build_stats
from config.startup_safety import assert_safe_startup
import historical_accelerator_v2_protocol as protocol
import historical_accelerator_v2 as engine

REST="https://api.bybit.com";HOUR_MS=3_600_000;SCHEMA="TRADINGCORE_HISTORICAL_V2_FORWARD_PAPER"
def now()->str:return datetime.now(timezone.utc).isoformat()
def atomic(p:Path,x:dict[str,Any])->None:p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+".tmp");t.write_text(json.dumps(x,indent=2,ensure_ascii=False,default=str),encoding="utf-8");t.replace(p)
def read(p:Path)->dict[str,Any]|None:
    if not p.exists():return None
    try:x=json.loads(p.read_text(encoding="utf-8-sig"));return x if isinstance(x,dict) else None
    except Exception:return None
def http(path:str,params:dict[str,Any])->dict[str,Any]:
    req=Request(f"{REST}{path}?{urlencode(params)}",headers={"Accept":"application/json","User-Agent":"TradingCore-HistV2Forward/1.0"})
    with urlopen(req,timeout=15) as h:x=json.loads(h.read().decode())
    if not isinstance(x,dict) or int(x.get("retCode",-1))!=0:raise RuntimeError(f"Bybit public request failed: {x}")
    return x
def latest_bars(symbol:str,hours:int=760)->list[engine.Bar]:
    end=(int(time.time()*1000)//HOUR_MS)*HOUR_MS+HOUR_MS-1;start=end-hours*HOUR_MS
    return engine.fetch_bars(symbol,start,end)
def state_default()->dict[str,Any]:return {"schema":SCHEMA,"last_signal_ts":0,"position":None,"closed_trades":[],"last_error":None,"real_order_sent":False}
def closed_objs(s:dict[str,Any])->list[ClosedTrade]:
    out=[]
    for r in s.get("closed_trades") or []:
        try:out.append(ClosedTrade(str(r["family_id"]),str(r["closed_at_utc"]),str(r["symbol"]),float(r["net_pnl"]),float(r["r_multiple"])))
        except Exception:pass
    return out
def status(s:dict[str,Any],auth:dict[str,Any]|None)->dict[str,Any]:
    st=build_stats(closed_objs(s));n=len(s.get("closed_trades") or []);passed=bool(n>=30 and isinstance(st.get("profit_factor"),(int,float)) and st["profit_factor"]>=1.15 and isinstance(st.get("expectancy_r"),(int,float)) and st["expectancy_r"]>0 and isinstance(st.get("max_drawdown_r"),(int,float)) and st["max_drawdown_r"]<=10)
    return {"schema":SCHEMA,"updated_at_utc":now(),"state":"WAITING_HISTORICAL_V2_CANDIDATE" if auth is None else ("FORWARD_PAPER_PASS_OWNER_REVIEW" if passed else "FORWARD_PAPER_RUNNING"),"authorized":auth is not None,"closed_trades":n,"required_closed_trades":30,"stats":st,"forward_pass":passed,"position":s.get("position"),"last_error":s.get("last_error"),"real_orders_enabled":False,"real_order_sent":False,"live_permission":False}
def manage_position(s:dict[str,Any],data:dict[str,engine.SymbolData],closed_bar_ts:int)->None:
    p=s.get("position")
    if not p:return
    sd=data.get(str(p["symbol"]));i=sd.index.get(closed_bar_ts) if sd else None
    if sd is None or i is None:return
    b=sd.bars[i];reason=None;exit_price=None
    if b.low<=float(p["stop"]):reason="STOP_LOSS";exit_price=float(p["stop"])
    elif b.high>=float(p["target"]):reason="TAKE_PROFIT";exit_price=float(p["target"])
    elif b.ts+HOUR_MS-int(p["entry_ms"])>=int(p["max_hold_hours"])*HOUR_MS:reason="TIME_STOP";exit_price=b.close
    if reason is None:return
    c=compute_trade_costs(entry_price=float(p["entry"]),exit_price=float(exit_price),quantity=float(p["quantity"]),side="LONG",config=TradingCostConfig());net=float(c["net_pnl"])
    s.setdefault("closed_trades",[]).append({"family_id":p["family_id"],"symbol":p["symbol"],"opened_at_utc":p["opened_at_utc"],"closed_at_utc":engine.utc(b.ts+HOUR_MS),"exit_reason":reason,"entry":p["entry"],"exit":exit_price,"quantity":p["quantity"],"net_pnl":net,"r_multiple":net/protocol.RISK_AMOUNT_USD,"real_order_sent":False});s["position"]=None
def maybe_enter(s:dict[str,Any],data:dict[str,engine.SymbolData],fam:dict[str,Any],closed_bar_ts:int,auth_ms:int)->None:
    if s.get("position") or closed_bar_ts<auth_ms or closed_bar_ts<=int(s.get("last_signal_ts") or 0):return
    candidates=[]
    for symbol,sd in data.items():
        i=sd.index.get(closed_bar_ts)
        if i is None:continue
        sc=engine.signal_score(sd,i,fam)
        if sc is not None:candidates.append((float(sc),symbol,i))
    s["last_signal_ts"]=closed_bar_ts
    if not candidates:return
    candidates.sort(key=lambda x:(-x[0],x[1]));score,symbol,i=candidates[0];sd=data[symbol];a=sd.atr14[i]
    if a is None or a<=0:return
    current_ts=closed_bar_ts+HOUR_MS;ei=sd.index.get(current_ts)
    if ei is None:return
    entry=sd.bars[ei].open;stop=entry-float(fam["atr_stop_multiple"])*a
    if stop<=0 or stop>=entry:return
    ru=entry-stop;qty=protocol.RISK_AMOUNT_USD/ru
    if qty*entry>protocol.REFERENCE_CAPITAL_USD*protocol.MAX_LEVERAGE+1e-9:return
    s["position"]={"mode":"PAPER","family_id":fam["id"],"symbol":symbol,"score":score,"entry_ms":current_ts,"opened_at_utc":engine.utc(current_ts),"entry":entry,"stop":stop,"target":entry+float(fam["risk_reward"])*ru,"quantity":qty,"max_hold_hours":int(fam["max_hold_hours"]),"real_order_sent":False}
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--state-dir",default="C:/TradingCore_Historical_Accelerator_V2");ap.add_argument("--universe-lock",default="C:/TradingCore_Collector_C/data/UNIVERSE_LOCK.json");ap.add_argument("--poll-seconds",type=int,default=60);args=ap.parse_args();assert_safe_startup();root=Path(args.state_dir);root.mkdir(parents=True,exist_ok=True);sp=root/"forward_paper_state.json";stp=root/"forward_paper_status.json";authp=root/"CANDIDATE_FOR_FORWARD_PAPER.json";owner=root/"OWNER_REVIEW_FOR_MICRO_LIVE.json";s=read(sp) or state_default()
    while True:
        try:
            auth=read(authp)
            if auth is None:s["last_error"]=None;atomic(sp,s);atomic(stp,status(s,None));time.sleep(max(30,args.poll_seconds));continue
            if auth.get("protocol_fingerprint")!=protocol.PROTOCOL_FINGERPRINT or auth.get("mode")!="PAPER_ONLY":raise RuntimeError("V2 auth mismatch")
            fam=auth.get("family") or {};lock=read(Path(args.universe_lock)) or {};symbols=[str(x) for x in lock.get("symbols") or []];data={}
            current_floor=(int(time.time()*1000)//HOUR_MS)*HOUR_MS;closed_ts=current_floor-HOUR_MS
            for sym in symbols:
                bars=latest_bars(sym);data[sym]=engine.precompute(sym,bars)
            manage_position(s,data,closed_ts);auth_ms=int(datetime.fromisoformat(str(auth["authorized_at_utc"])).timestamp()*1000);maybe_enter(s,data,fam,closed_ts,auth_ms)
            s["last_error"]=None;s["real_order_sent"]=False;atomic(sp,s);stat=status(s,auth);atomic(stp,stat)
            if stat.get("forward_pass") and not owner.exists():atomic(owner,{"schema":"TRADINGCORE_HISTORICAL_V2_OWNER_REVIEW","created_at_utc":now(),"state":"OWNER_REVIEW_REQUIRED","forward_stats":stat.get("stats"),"live_enabled":False,"real_orders_enabled":False,"note":"Historical V2 final holdout + forward PAPER passed. LIVE remains disabled pending separate micro-live execution architecture and explicit owner approval."})
        except Exception as e:
            s["last_error"]=f"{type(e).__name__}: {e}";s["real_order_sent"]=False;atomic(sp,s);atomic(stp,{"schema":SCHEMA,"state":"FAILED_SAFELY_RETRYING","last_error":s["last_error"],"real_orders_enabled":False,"real_order_sent":False,"live_permission":False,"updated_at_utc":now()})
        time.sleep(max(30,args.poll_seconds))
if __name__=="__main__":raise SystemExit(main())
