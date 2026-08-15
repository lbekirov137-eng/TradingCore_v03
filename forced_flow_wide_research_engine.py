#!/usr/bin/env python3
"""Preregistered wide forced-flow V2 research engine.

Selects an evidence epoch ONLY by predeclared data-volume/integrity criteria,
then opens outcomes once. Uses public Bybit SPOT 1m prices because the eventual
candidate is spot-long-only. No private API and no order path.
"""
from __future__ import annotations

import argparse, json, math, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from api.strategy_supervisor.validation import validate_candidate
from config.startup_safety import assert_safe_startup
import forced_flow_wide_protocol as protocol

SCHEMA="TRADINGCORE_FORCED_FLOW_WIDE_RESEARCH_V2"
REST="https://api.bybit.com"
MINUTE_MS=60_000

@dataclass(frozen=True)
class Event:
    ts:int; symbol:str; side:str; notional:float; key:str; turnover_bps:float
@dataclass(frozen=True)
class Cluster:
    symbol:str; side:str; start_ms:int; end_ms:int; event_count:int; notional:float; turnover_bps:float; keys:tuple[str,...]
@dataclass(frozen=True)
class Candle:
    start_ms:int; open:float; high:float; low:float; close:float

def utc(ms:int)->str:return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()
def ceil_minute(ms:int)->int:return ((ms+MINUTE_MS-1)//MINUTE_MS)*MINUTE_MS

def http_json(path:str,params:dict[str,Any])->dict[str,Any]:
    req=Request(f"{REST}{path}?{urlencode(params)}",headers={"Accept":"application/json","User-Agent":"TradingCore-WideResearch/2.0"})
    with urlopen(req,timeout=20) as r:p=json.loads(r.read().decode())
    if not isinstance(p,dict) or int(p.get("retCode",-1))!=0:raise RuntimeError(f"Bybit public REST failed: {p}")
    return p

def read_lock(data:Path)->dict[str,Any]:
    p=json.loads((data/"UNIVERSE_LOCK.json").read_text(encoding="utf-8-sig"))
    if p.get("schema")!="TRADINGCORE_COLLECTOR_C_UNIVERSE_LOCK_V1":raise RuntimeError("Universe lock mismatch")
    return p

def read_epoch(epoch:Path,lock:dict[str,Any])->tuple[list[Event],dict[str,Any]]:
    symbols=set(str(s).upper() for s in lock["symbols"]); fp=str(lock["fingerprint"]); turns={str(k):float(v) for k,v in lock["turnover24h_usdt_at_lock"].items()}
    seen:set[str]=set(); events:list[Event]=[]; invalid=duplicates=0
    files=sorted((epoch/"normalized").glob("*.jsonl")) if (epoch/"normalized").exists() else []
    for path in files:
        with path.open("r",encoding="utf-8") as h:
            for line in h:
                if not line.strip():continue
                try:r=json.loads(line)
                except json.JSONDecodeError:invalid+=1;continue
                if not isinstance(r,dict) or r.get("schema")!=protocol.COLLECTOR_SCHEMA or r.get("cohort")!=protocol.COLLECTOR_COHORT or r.get("universe_fingerprint")!=fp:invalid+=1;continue
                symbol=str(r.get("symbol") or "").upper();side=str(r.get("liquidated_position_side") or "").upper();key=str(r.get("event_key") or "")
                try:ts=int(r.get("event_ts_ms"));size=float(r.get("size_raw"));price=float(r.get("bankruptcy_price_raw"));turnover=turns[symbol]
                except (TypeError,ValueError,KeyError):invalid+=1;continue
                if symbol not in symbols or side not in ("LONG","SHORT") or not key or ts<=0 or size<=0 or price<=0 or turnover<=0:invalid+=1;continue
                if key in seen:duplicates+=1;continue
                seen.add(key);notional=size*price;events.append(Event(ts,symbol,side,notional,key,notional/turnover*10_000.0))
    events.sort(key=lambda e:(e.ts,e.symbol,e.side,e.key));span=0.0 if len(events)<2 else (events[-1].ts-events[0].ts)/3_600_000.0
    return events,{"epoch_id":epoch.name,"files":len(files),"valid_events":len(events),"invalid_records":invalid,"duplicates":duplicates,"span_hours":round(span,4)}

def cluster(events:list[Event])->list[Cluster]:
    groups:dict[tuple[str,str],list[Event]]={};window=protocol.CLUSTER_WINDOW_SECONDS*1000
    for e in events:groups.setdefault((e.symbol,e.side),[]).append(e)
    out:list[Cluster]=[]
    for (symbol,side),rows in groups.items():
        rows.sort(key=lambda e:e.ts);cur:list[Event]=[]
        def emit(items:list[Event])->None:
            if not items:return
            n=sum(x.notional for x in items); # turnover bps sums because denominator fixed per symbol
            out.append(Cluster(symbol,side,items[0].ts,items[-1].ts,len(items),n,sum(x.turnover_bps for x in items),tuple(x.key for x in items)))
        for e in rows:
            if not cur or e.ts-cur[-1].ts<=window:cur.append(e)
            else:emit(cur);cur=[e]
        emit(cur)
    return sorted(out,key=lambda c:(c.end_ms,c.symbol,c.side))

def epoch_readiness(events:list[Event],clusters:list[Cluster],meta:dict[str,Any])->dict[str,Any]:
    primary=[c for c in clusters if c.side==protocol.LIQUIDATED_SIDE and c.turnover_bps>=protocol.PRIMARY_THRESHOLD_TURNOVER_BPS]
    represented=sorted({c.symbol for c in primary});missing=[]
    if meta["invalid_records"]:missing.append("INVALID_RECORDS")
    if meta["duplicates"]:missing.append("DUPLICATES")
    if len(events)<protocol.MIN_VALID_EVENTS_FOR_RESEARCH:missing.append(f"events={len(events)}<{protocol.MIN_VALID_EVENTS_FOR_RESEARCH}")
    if meta["span_hours"]<protocol.MIN_OBSERVATION_SPAN_HOURS:missing.append(f"span_hours={meta['span_hours']}<{protocol.MIN_OBSERVATION_SPAN_HOURS}")
    if len(primary)<protocol.MIN_PRIMARY_CLUSTERS_FOR_RESEARCH:missing.append(f"primary_clusters={len(primary)}<{protocol.MIN_PRIMARY_CLUSTERS_FOR_RESEARCH}")
    if len(represented)<protocol.MIN_PRIMARY_SYMBOLS_REPRESENTED:missing.append(f"primary_symbols={len(represented)}<{protocol.MIN_PRIMARY_SYMBOLS_REPRESENTED}")
    return {**meta,"primary_clusters":len(primary),"primary_symbols":represented,"ready":not missing,"missing":missing}

def select_epoch(data:Path,lock:dict[str,Any])->tuple[Path|None,list[Event],list[Cluster],list[dict[str,Any]]]:
    reports=[];chosen=None;chosen_events=[];chosen_clusters=[]
    for epoch in sorted((data/"epochs").glob("EPOCH_*")):
        if not epoch.is_dir():continue
        events,meta=read_epoch(epoch,lock);clusters=cluster(events);report=epoch_readiness(events,clusters,meta);reports.append(report)
        if chosen is None and report["ready"]:chosen=epoch;chosen_events=events;chosen_clusters=clusters
    return chosen,chosen_events,chosen_clusters,reports

def fetch_klines(symbol:str,start:int,end:int)->list[Candle]:
    cursor=end;by:dict[int,Candle]={}
    for _ in range(500):
        p=http_json("/v5/market/kline",{"category":protocol.PRICE_CATEGORY,"symbol":symbol,"interval":protocol.PRICE_INTERVAL,"start":max(0,start),"end":cursor,"limit":1000});rows=((p.get("result") or {}).get("list") or [])
        if not rows:break
        oldest=None
        for r in rows:
            if not isinstance(r,list) or len(r)<5:continue
            try:c=Candle(int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]))
            except (TypeError,ValueError):continue
            if start<=c.start_ms<=end:by[c.start_ms]=c;oldest=c.start_ms if oldest is None else min(oldest,c.start_ms)
        if oldest is None or oldest<=start:break
        cursor=oldest-1;time.sleep(0.04)
    return sorted(by.values(),key=lambda c:c.start_ms)
def atr14(prior:list[Candle])->float|None:
    if len(prior)<protocol.ATR_PERIOD+1:return None
    rows=prior[-(protocol.ATR_PERIOD+1):];trs=[]
    for i in range(1,len(rows)):
        c,p=rows[i],rows[i-1];trs.append(max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)))
    return sum(trs)/len(trs) if len(trs)==protocol.ATR_PERIOD else None

def simulate(clusters:list[Cluster],prices:dict[str,list[Candle]],threshold:float)->tuple[list[ClosedTrade],list[dict[str,Any]]]:
    q=[c for c in clusters if c.side==protocol.LIQUIDATED_SIDE and c.turnover_bps>=threshold];q.sort(key=lambda c:c.end_ms);indexes={s:{c.start_ms:i for i,c in enumerate(rows)} for s,rows in prices.items()};busy={s:0 for s in prices};trades=[];details=[];costs=TradingCostConfig()
    for cl in q:
        if cl.end_ms<busy.get(cl.symbol,0):continue
        rows=prices.get(cl.symbol) or [];idx=indexes.get(cl.symbol,{}).get(ceil_minute(cl.end_ms))
        if idx is None or idx<protocol.ATR_PERIOD+1:continue
        if not rows[idx].close>rows[idx].open:continue
        entry_i=idx+protocol.STABILISATION_CANDLES
        if entry_i>=len(rows):continue
        a=atr14(rows[:idx]);
        if a is None or a<=0:continue
        entry=rows[entry_i].open;stop=entry-protocol.ATR_STOP_MULTIPLE*a
        if stop<=0 or stop>=entry:continue
        risk_unit=entry-stop;qty=protocol.RISK_AMOUNT_USD/risk_unit;notional=qty*entry
        if notional>protocol.REFERENCE_CAPITAL_USD*protocol.MAX_LEVERAGE+1e-9:continue
        target=entry+protocol.RISK_REWARD*risk_unit;end_i=min(len(rows)-1,entry_i+protocol.MAX_HOLD_MINUTES-1);exit_price=None;reason=None;exit_i=None
        for i in range(entry_i,end_i+1):
            c=rows[i]
            if c.low<=stop:exit_price=stop;reason="STOP_LOSS";exit_i=i;break
            if c.high>=target:exit_price=target;reason="TAKE_PROFIT";exit_i=i;break
        if exit_price is None:exit_i=end_i;exit_price=rows[exit_i].close;reason="TIME_STOP"
        result=compute_trade_costs(entry_price=entry,exit_price=float(exit_price),quantity=qty,side="LONG",config=costs);net=float(result["net_pnl"]);closed_ms=rows[exit_i].start_ms+MINUTE_MS
        trades.append(ClosedTrade(protocol.STRATEGY_ID,utc(closed_ms),cl.symbol,net,net/protocol.RISK_AMOUNT_USD));busy[cl.symbol]=closed_ms
        details.append({"symbol":cl.symbol,"cluster_end_utc":utc(cl.end_ms),"cluster_events":cl.event_count,"cluster_notional_usdt":round(cl.notional,2),"cluster_turnover_bps":round(cl.turnover_bps,6),"threshold_turnover_bps":threshold,"entry":entry,"stop":stop,"target":target,"exit":exit_price,"exit_reason":reason,"net_pnl":net,"r_multiple":net/protocol.RISK_AMOUNT_USD,"real_order_sent":False})
    return trades,details

def write(out:Path,report:dict[str,Any])->None:
    out.mkdir(parents=True,exist_ok=True);stamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S");payload=json.dumps(report,indent=2,ensure_ascii=False,default=str);(out/f"wide_research_{stamp}.json").write_text(payload,encoding="utf-8");(out/"LATEST_FORCED_FLOW_WIDE_RESEARCH.json").write_text(payload,encoding="utf-8")
    print("="*92);print("TRADINGCORE FORCED-FLOW WIDE V2 PREREGISTERED RESEARCH");print("State:",report.get("state"));print("Selected epoch:",report.get("selected_epoch"));print("Readiness:",report.get("readiness_missing"));g=report.get("promotion_gates") or {};v=report.get("validation") or {};print(f"OOS trades={v.get('oos_trades')} PF={v.get('oos_profit_factor')} expR={v.get('oos_expectancy_r')} net={v.get('oos_net_pnl')} DD={v.get('oos_max_drawdown_r')}");print("Promotion:",g.get("passed"),"failed=",g.get("failed_gates"));print("Orders/LIVE: DISABLED | Collector A/B: UNCHANGED");print("="*92)

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--data-dir",default="C:/TradingCore_Collector_C/data");ap.add_argument("--output",default="forced_flow_wide_research_results");args=ap.parse_args();safety=assert_safe_startup();data=Path(args.data_dir);out=Path(args.output);out=out if out.is_absolute() else Path.cwd()/out;lock=read_lock(data);selected,events,clusters,reports=select_epoch(data,lock)
    base={"schema":SCHEMA,"generated_at_utc":datetime.now(timezone.utc).isoformat(),"mode":"RESEARCH_ONLY","protocol":{**protocol.protocol_dict(),"fingerprint":protocol.PROTOCOL_FINGERPRINT},"universe_lock_fingerprint":lock.get("fingerprint"),"universe_symbols":lock.get("symbols"),"safety":safety,"epoch_readiness_reports":reports,"private_api_used":False,"real_orders_enabled":False,"collector_a_modified":False,"collector_b_modified":False}
    if selected is None:
        best=max(reports,key=lambda r:(r.get("valid_events",0),r.get("span_hours",0)),default={});base.update(state="WAITING_FOR_PREREGISTERED_EPOCH",selected_epoch=None,readiness_missing=best.get("missing") or ["NO_EPOCH"]);write(out,base);return 0
    primary=[c for c in clusters if c.side==protocol.LIQUIDATED_SIDE and c.turnover_bps>=protocol.PRIMARY_THRESHOLD_TURNOVER_BPS];minth=min(protocol.THRESHOLD_COHORTS_TURNOVER_BPS);relevant=[c for c in clusters if c.side==protocol.LIQUIDATED_SIDE and c.turnover_bps>=minth];prices={}
    for symbol in lock["symbols"]:
        rows=[c for c in relevant if c.symbol==symbol]
        if not rows:prices[symbol]=[];continue
        start=min(c.end_ms for c in rows)-60*MINUTE_MS;end=min(max(c.end_ms for c in rows)+60*MINUTE_MS,int(time.time()*1000)-MINUTE_MS);prices[symbol]=fetch_klines(symbol,start,end)
    threshold_results={};primary_trades=[];primary_details=[]
    for th in protocol.THRESHOLD_COHORTS_TURNOVER_BPS:
        trades,details=simulate(clusters,prices,th);threshold_results[str(th)]={"trades":len(trades),"stats_full_descriptive":build_stats(trades)}
        if th==protocol.PRIMARY_THRESHOLD_TURNOVER_BPS:primary_trades,primary_details=trades,details
    if len(primary_trades)<2:base.update(state="INSUFFICIENT_TRADE_GEOMETRY",selected_epoch=selected.name,primary_trade_count=len(primary_trades),threshold_results=threshold_results);write(out,base);return 0
    sample_id=f"{protocol.PROTOCOL_VERSION}:{protocol.PROTOCOL_FINGERPRINT[:12]}:{lock['fingerprint'][:12]}:{selected.name}"
    validation=validate_candidate(protocol.STRATEGY_ID,primary_trades,sample_id=sample_id,holdout_fraction=protocol.HOLDOUT_FRACTION,window_count=4,safety_violations=());holdout=validation.get("holdout_start_utc")
    cross={};profitable_thresholds=0
    for th in protocol.THRESHOLD_COHORTS_TURNOVER_BPS:
        trades,_=simulate(clusters,prices,th);oos=[t for t in trades if isinstance(holdout,str) and isinstance(t.closed_at_utc,str) and t.closed_at_utc>=holdout];st=build_stats(oos);prof=bool(st.get("net_pnl") is not None and st["net_pnl"]>0 and st.get("expectancy_r") is not None and st["expectancy_r"]>0);profitable_thresholds+=int(prof);cross[str(th)]={"trades":len(oos),"stats":st,"profitable":prof}
    primary_oos=[t for t in primary_trades if isinstance(holdout,str) and isinstance(t.closed_at_utc,str) and t.closed_at_utc>=holdout];by_symbol={}
    for symbol in lock["symbols"]:
        rows=[t for t in primary_oos if t.regime==symbol]
        if len(rows)>=protocol.MIN_OOS_TRADES_PER_SYMBOL:
            st=build_stats(rows);by_symbol[symbol]={"trades":len(rows),"stats":st,"profitable":bool(st.get("net_pnl") is not None and st["net_pnl"]>0 and st.get("expectancy_r") is not None and st["expectancy_r"]>0)}
    eval_count=len(by_symbol);prof_symbols=sum(1 for x in by_symbol.values() if x["profitable"]);symbol_ratio=(prof_symbols/eval_count) if eval_count else 0.0
    validation["cross_threshold_profitable_cohorts"]=profitable_thresholds;validation["symbol_oos_evaluable"]=eval_count;validation["symbol_oos_profitable"]=prof_symbols;validation["symbol_oos_profitable_ratio"]=round(symbol_ratio,4)
    gates=promotion_gates(validation)
    def add_gate(name:str,passed:bool,detail:str):
        gates["checks"].append({"gate":name,"passed":passed,"detail":detail});
        if not passed and name not in gates["failed_gates"]:gates["failed_gates"].append(name);gates["passed"]=False
    add_gate("cross_threshold_robustness",profitable_thresholds>=protocol.MIN_PROFITABLE_THRESHOLD_COHORTS,f"{profitable_thresholds}/{len(protocol.THRESHOLD_COHORTS_TURNOVER_BPS)} profitable threshold cohorts")
    add_gate("cross_symbol_evaluable",eval_count>=protocol.MIN_EVALUABLE_SYMBOLS_OOS,f"{eval_count} evaluable symbols; required {protocol.MIN_EVALUABLE_SYMBOLS_OOS}")
    add_gate("cross_symbol_profitability",eval_count>=protocol.MIN_EVALUABLE_SYMBOLS_OOS and symbol_ratio>=protocol.MIN_PROFITABLE_SYMBOL_RATIO,f"{prof_symbols}/{eval_count} profitable = {symbol_ratio:.3f}; required >= {protocol.MIN_PROFITABLE_SYMBOL_RATIO}")
    state="HISTORICAL_PROMOTION_PASS" if gates["passed"] else "HISTORICAL_REJECT_FROZEN"
    base.update(state=state,selected_epoch=selected.name,readiness_missing=[],sample_id=sample_id,event_count=len(events),primary_clusters=len(primary),primary_trade_count=len(primary_trades),price_source="BYBIT_PUBLIC_SPOT_1M_NO_API_KEY",threshold_results=threshold_results,cross_threshold_oos=cross,cross_symbol_oos=by_symbol,validation=validation,promotion_gates=gates,primary_trade_details=primary_details,next_gate="FORWARD_PAPER_CONFIRMATION" if gates["passed"] else "PROTOCOL_V3_OR_NEW_INDEPENDENT_HYPOTHESIS")
    write(out,base);return 0

if __name__=="__main__":raise SystemExit(main())
