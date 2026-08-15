#!/usr/bin/env python3
"""Forward PAPER worker for Wide V2. Inert until historical PASS marker exists.
No private API, account access, order client, or LIVE path.
"""
from __future__ import annotations
import argparse,hashlib,json,math,time
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request,urlopen

from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade,build_stats
from config.startup_safety import assert_safe_startup
import forced_flow_wide_protocol as protocol
import forced_flow_wide_research_engine as engine

SCHEMA="TRADINGCORE_FORCED_FLOW_WIDE_FORWARD_PAPER_V2";REST="https://api.bybit.com";MINUTE_MS=60_000
def now()->str:return datetime.now(timezone.utc).isoformat()
def parse_ms(v:str)->int:return int(datetime.fromisoformat(v).timestamp()*1000)
def atomic(p:Path,x:dict[str,Any])->None:p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+".tmp");t.write_text(json.dumps(x,indent=2,ensure_ascii=False,default=str),encoding="utf-8");t.replace(p)
def read(p:Path)->dict[str,Any]|None:
    if not p.exists():return None
    try:x=json.loads(p.read_text(encoding="utf-8-sig"));return x if isinstance(x,dict) else None
    except Exception:return None
def http(path:str,params:dict[str,Any])->dict[str,Any]:
    r=Request(f"{REST}{path}?{urlencode(params)}",headers={"Accept":"application/json","User-Agent":"TradingCore-WideForwardPaper/2.0"})
    with urlopen(r,timeout=15) as h:x=json.loads(h.read().decode())
    if not isinstance(x,dict) or int(x.get("retCode",-1))!=0:raise RuntimeError(f"Bybit public request failed: {x}")
    return x
def spot_price(symbol:str)->float:
    rows=((http("/v5/market/tickers",{"category":"spot","symbol":symbol}).get("result") or {}).get("list") or [])
    if not rows:raise RuntimeError(f"No spot ticker {symbol}")
    p=float(rows[0]["lastPrice"])
    if not math.isfinite(p) or p<=0:raise RuntimeError("Invalid spot ticker")
    return p

def load_events_by_epoch(data:Path,lock:dict[str,Any],freeze_ms:int)->list[tuple[str,engine.Event]]:
    out=[];turns={str(k):float(v) for k,v in lock["turnover24h_usdt_at_lock"].items()};symbols=set(lock["symbols"]);fp=lock["fingerprint"]
    for ep in sorted((data/"epochs").glob("EPOCH_*")):
        seen=set()
        for f in sorted((ep/"normalized").glob("*.jsonl")) if (ep/"normalized").exists() else []:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():continue
                try:r=json.loads(line)
                except json.JSONDecodeError:continue
                if not isinstance(r,dict) or r.get("schema")!=protocol.COLLECTOR_SCHEMA or r.get("universe_fingerprint")!=fp:continue
                s=str(r.get("symbol") or "").upper();side=str(r.get("liquidated_position_side") or "").upper();key=str(r.get("event_key") or "")
                try:ts=int(r.get("event_ts_ms"));size=float(r.get("size_raw"));price=float(r.get("bankruptcy_price_raw"));turn=turns[s]
                except (TypeError,ValueError,KeyError):continue
                if s not in symbols or side not in ("LONG","SHORT") or ts<=freeze_ms or key in seen or size<=0 or price<=0 or turn<=0:continue
                seen.add(key);notional=size*price;out.append((ep.name,engine.Event(ts,s,side,notional,key,notional/turn*10_000)))
    out.sort(key=lambda x:(x[1].ts,x[0],x[1].symbol,x[1].key));return out
def clusters_after_freeze(data:Path,lock:dict[str,Any],freeze_ms:int)->list[tuple[str,engine.Cluster]]:
    pairs=load_events_by_epoch(data,lock,freeze_ms);groups={}
    for ep,e in pairs:groups.setdefault(ep,[]).append(e)
    out=[]
    for ep,events in groups.items():
        for c in engine.cluster(events):out.append((ep,c))
    out.sort(key=lambda x:(x[1].end_ms,x[0],x[1].symbol));return out
def cid(ep:str,c:engine.Cluster)->str:return hashlib.sha256((ep+"|"+"|".join(c.keys)).encode()).hexdigest()[:24]
def default_state(fp:str,ufp:str)->dict[str,Any]:return {"schema":SCHEMA,"protocol_fingerprint":fp,"universe_fingerprint":ufp,"processed":[],"pending":None,"position":None,"closed_trades":[],"skipped":{},"last_error":None,"real_order_sent":False}
def load_state(path:Path,fp:str,ufp:str)->dict[str,Any]:
    s=read(path)
    if s is None:return default_state(fp,ufp)
    if s.get("schema")!=SCHEMA or s.get("protocol_fingerprint")!=fp or s.get("universe_fingerprint")!=ufp or s.get("real_order_sent") is not False:raise RuntimeError("Unsafe/mismatched Wide forward state")
    return s
def trades_obj(s:dict[str,Any])->list[ClosedTrade]:
    out=[]
    for r in s.get("closed_trades") or []:
        try:out.append(ClosedTrade(protocol.STRATEGY_ID,str(r["closed_at_utc"]),str(r["symbol"]),float(r["net_pnl"]),float(r["r_multiple"])))
        except Exception:pass
    return out
def status(s:dict[str,Any],auth:dict[str,Any]|None,lock:dict[str,Any]|None)->dict[str,Any]:
    ts=trades_obj(s);st=build_stats(ts);hv=(lock or {}).get("validation") or {};val={"oos_trades":st.get("closed_trades"),"oos_net_pnl":st.get("net_pnl"),"oos_profit_factor":st.get("profit_factor"),"oos_expectancy_r":st.get("expectancy_r"),"oos_max_drawdown_r":st.get("max_drawdown_r"),"safety_violations":[],"robustness_ratio":hv.get("robustness_ratio"),"walk_forward_passed":hv.get("walk_forward_passed") is True,"look_ahead_leakage":False};g=promotion_gates(val) if ts else {"passed":False,"failed_gates":["min_oos_trades"],"checks":[]};passed=len(ts)>=protocol.FORWARD_PAPER_MIN_CLOSED_TRADES and bool(g.get("passed"))
    return {"schema":SCHEMA,"updated_at_utc":now(),"state":"WAITING_HISTORICAL_RESEARCH_PASS" if auth is None else ("FORWARD_PAPER_PASS_OWNER_REVIEW" if passed else "FORWARD_PAPER_RUNNING"),"mode":"PAPER_ONLY","authorized":auth is not None,"closed_trades":len(ts),"required_closed_trades":protocol.FORWARD_PAPER_MIN_CLOSED_TRADES,"stats":st,"promotion_gates_on_forward":g,"forward_pass":passed,"pending":s.get("pending"),"position":s.get("position"),"last_error":s.get("last_error"),"private_api_used":False,"real_orders_enabled":False,"real_order_sent":False,"live_permission":False,"collector_a_modified":False,"collector_b_modified":False}
def process_clusters(s:dict[str,Any],data:Path,lock:dict[str,Any],freeze:int)->None:
    if s.get("position") or s.get("pending"):return
    done=set(s.get("processed") or []);nowms=int(time.time()*1000)
    for ep,c in clusters_after_freeze(data,lock,freeze):
        if c.side!=protocol.LIQUIDATED_SIDE or c.turnover_bps<protocol.PRIMARY_THRESHOLD_TURNOVER_BPS:continue
        if nowms-c.end_ms<protocol.CLUSTER_WINDOW_SECONDS*1000:continue
        k=cid(ep,c)
        if k in done:continue
        done.add(k);s["pending"]={"cluster_id":k,"epoch_id":ep,"symbol":c.symbol,"cluster_end_ms":c.end_ms,"cluster_turnover_bps":c.turnover_bps,"created_at_utc":now()};break
    s["processed"]=list(done)[-20000:]
def process_pending(s:dict[str,Any])->None:
    p=s.get("pending")
    if not p:return
    nowms=int(time.time()*1000);stab=engine.ceil_minute(int(p["cluster_end_ms"]));entry_start=stab+MINUTE_MS
    if nowms<entry_start:return
    if nowms>entry_start+15_000:s.setdefault("skipped",{})[p["cluster_id"]]="MISSED_15S_ENTRY_WINDOW";s["pending"]=None;return
    rows=engine.fetch_klines(p["symbol"],stab-40*MINUTE_MS,stab+MINUTE_MS);by={r.start_ms:i for i,r in enumerate(rows)};i=by.get(stab)
    if i is None or i<protocol.ATR_PERIOD+1:return
    if not rows[i].close>rows[i].open:s.setdefault("skipped",{})[p["cluster_id"]]="STABILISATION_NOT_BULLISH";s["pending"]=None;return
    a=engine.atr14(rows[:i])
    if a is None or a<=0:return
    entry=spot_price(p["symbol"]);stop=entry-protocol.ATR_STOP_MULTIPLE*a
    if stop<=0 or stop>=entry:s["pending"]=None;return
    ru=entry-stop;qty=protocol.RISK_AMOUNT_USD/ru;notional=qty*entry
    if notional>protocol.REFERENCE_CAPITAL_USD*protocol.MAX_LEVERAGE+1e-9:s.setdefault("skipped",{})[p["cluster_id"]]="STOP_TOO_TIGHT_FOR_1X";s["pending"]=None;return
    s["position"]={"mode":"PAPER","side":"LONG","symbol":p["symbol"],"cluster_id":p["cluster_id"],"entry":entry,"stop":stop,"target":entry+protocol.RISK_REWARD*ru,"quantity":qty,"risk_amount":protocol.RISK_AMOUNT_USD,"position_notional":notional,"opened_ms":nowms,"opened_at_utc":now(),"real_order_sent":False};s["pending"]=None
def process_position(s:dict[str,Any])->None:
    p=s.get("position")
    if not p:return
    price=spot_price(p["symbol"]);nowms=int(time.time()*1000);reason=None
    if price<=float(p["stop"]):reason="STOP_LOSS"
    elif price>=float(p["target"]):reason="TAKE_PROFIT"
    elif nowms-int(p["opened_ms"])>=protocol.MAX_HOLD_MINUTES*MINUTE_MS:reason="TIME_STOP"
    if reason is None:p["last_price"]=price;p["last_update_utc"]=now();return
    res=compute_trade_costs(entry_price=float(p["entry"]),exit_price=price,quantity=float(p["quantity"]),side="LONG",config=TradingCostConfig());net=float(res["net_pnl"]);s.setdefault("closed_trades",[]).append({"symbol":p["symbol"],"cluster_id":p["cluster_id"],"opened_at_utc":p["opened_at_utc"],"closed_at_utc":now(),"exit_reason":reason,"entry":p["entry"],"exit":price,"quantity":p["quantity"],"net_pnl":net,"r_multiple":net/protocol.RISK_AMOUNT_USD,"real_order_sent":False});s["position"]=None
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--data-dir",default="C:/TradingCore_Collector_C/data");ap.add_argument("--state-dir",default="C:/TradingCore_Wide_Autonomous");ap.add_argument("--poll-seconds",type=int,default=2);args=ap.parse_args();assert_safe_startup();data=Path(args.data_dir);root=Path(args.state_dir);root.mkdir(parents=True,exist_ok=True);lock=read(data/"UNIVERSE_LOCK.json")
    if lock is None:raise SystemExit("Universe lock missing")
    ufp=str(lock["fingerprint"]);sp=root/"forward_paper_state.json";stp=root/"forward_paper_status.json";authp=root/"FORWARD_PAPER_AUTHORIZED_BY_RESEARCH.json";decisionp=root/"historical_decision_lock.json";owner=root/"OWNER_REVIEW_FOR_MICRO_LIVE.json";s=load_state(sp,protocol.PROTOCOL_FINGERPRINT,ufp)
    while True:
        try:
            auth=read(authp);decision=read(decisionp)
            if auth:
                if auth.get("protocol_fingerprint")!=protocol.PROTOCOL_FINGERPRINT or auth.get("universe_fingerprint")!=ufp or auth.get("mode")!="PAPER_ONLY":raise RuntimeError("Wide forward auth mismatch")
                freeze=parse_ms(str(auth["authorized_at_utc"]));process_clusters(s,data,lock,freeze);process_pending(s);process_position(s)
            s["last_error"]=None;s["real_order_sent"]=False;atomic(sp,s);stat=status(s,auth,decision);atomic(stp,stat)
            if stat.get("forward_pass") is True and not owner.exists():atomic(owner,{"schema":"TRADINGCORE_WIDE_OWNER_MICRO_LIVE_REVIEW_V2","created_at_utc":now(),"state":"OWNER_REVIEW_REQUIRED","protocol_version":protocol.PROTOCOL_VERSION,"forward_closed_trades":stat.get("closed_trades"),"forward_stats":stat.get("stats"),"live_enabled":False,"real_orders_enabled":False,"note":"Historical + forward PAPER gates passed. LIVE remains disabled; separate execution architecture and explicit owner approval required."})
        except Exception as e:s["last_error"]=f"{type(e).__name__}: {e}";s["real_order_sent"]=False;atomic(sp,s);atomic(stp,{"schema":SCHEMA,"state":"FAILED_SAFELY_RETRYING","last_error":s["last_error"],"real_orders_enabled":False,"real_order_sent":False,"live_permission":False,"updated_at_utc":now()})
        time.sleep(max(2,args.poll_seconds))
if __name__=="__main__":raise SystemExit(main())
