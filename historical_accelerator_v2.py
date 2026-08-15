#!/usr/bin/env python3
"""TradingCore Historical Accelerator V2 — independent price-only research.

V1 remains sealed. V2 uses the already-downloaded public spot 1h price cache,
three preregistered families, a pre-final family-selection stage, then a sealed
final 20% holdout opened only for the first preregistered family that passes.
No private API, no order path, no LIVE permission.
"""
from __future__ import annotations

import argparse, bisect, gzip, json, math, statistics, time
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
import historical_accelerator_v2_protocol as protocol

REST = "https://api.bybit.com"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
SCHEMA = "TRADINGCORE_HISTORICAL_ACCELERATOR_V2"

@dataclass(frozen=True)
class Bar:
    ts:int; open:float; high:float; low:float; close:float; volume:float; turnover:float

@dataclass
class SymbolData:
    symbol:str
    bars:list[Bar]
    index:dict[int,int]
    ema168:list[float|None]
    atr14:list[float|None]
    compression:list[bool]
    prev24_high:list[float|None]
    prev24_turnover_median:list[float|None]

@dataclass
class Trade:
    family_id:str; symbol:str; signal_ms:int; entry_ms:int; closed_ms:int
    entry:float; stop:float; target:float; exit:float; exit_reason:str
    quantity:float; net_pnl:float; r_multiple:float; score:float
    def closed_trade(self)->ClosedTrade:
        return ClosedTrade(self.family_id, utc(self.closed_ms), self.symbol, self.net_pnl, self.r_multiple)
    def to_dict(self)->dict[str,Any]:
        return {"family_id":self.family_id,"symbol":self.symbol,"signal_utc":utc(self.signal_ms),
                "entry_utc":utc(self.entry_ms),"closed_utc":utc(self.closed_ms),"entry":self.entry,
                "stop":self.stop,"target":self.target,"exit":self.exit,"exit_reason":self.exit_reason,
                "quantity":self.quantity,"net_pnl":self.net_pnl,"r_multiple":self.r_multiple,
                "score":self.score,"real_order_sent":False}

def utc(ms:int)->str:
    return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()

def read_json(path:Path)->dict[str,Any]|None:
    if not path.exists(): return None
    try:
        x=json.loads(path.read_text(encoding="utf-8-sig")); return x if isinstance(x,dict) else None
    except Exception:return None

def atomic(path:Path,payload:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False,default=str),encoding="utf-8");tmp.replace(path)

def cache_bars(path:Path)->list[Bar]:
    if not path.exists(): return []
    try:
        with gzip.open(path,"rt",encoding="utf-8") as h:p=json.load(h)
        rows=p.get("bars") if isinstance(p,dict) else None
        out=[]
        for r in rows or []:
            try:out.append(Bar(int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]),float(r[6])))
            except (TypeError,ValueError,IndexError):pass
        return sorted({b.ts:b for b in out}.values(),key=lambda b:b.ts)
    except Exception:return []

def http_json(path:str,params:dict[str,Any])->dict[str,Any]:
    req=Request(f"{REST}{path}?{urlencode(params)}",headers={"Accept":"application/json","User-Agent":"TradingCore-HistV2/1.0"})
    with urlopen(req,timeout=25) as h:p=json.loads(h.read().decode())
    if not isinstance(p,dict) or int(p.get("retCode",-1))!=0:raise RuntimeError(f"Bybit public request failed: {p}")
    return p

def fetch_bars(symbol:str,start:int,end:int)->list[Bar]:
    by={};cursor=end
    for _ in range(100):
        p=http_json("/v5/market/kline",{"category":"spot","symbol":symbol,"interval":"60","start":start,"end":cursor,"limit":1000})
        rows=((p.get("result") or {}).get("list") or [])
        if not rows:break
        oldest=None
        for r in rows:
            if not isinstance(r,list) or len(r)<7:continue
            try:b=Bar(int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]),float(r[6]))
            except (TypeError,ValueError):continue
            if start<=b.ts<=end:by[b.ts]=b;oldest=b.ts if oldest is None else min(oldest,b.ts)
        if oldest is None or oldest<=start or len(rows)<1000:break
        cursor=oldest-1;time.sleep(0.05)
    return sorted(by.values(),key=lambda b:b.ts)

def ema(values:list[float],period:int)->list[float|None]:
    out:[float|None]=[None]*len(values)
    if len(values)<period:return out
    alpha=2.0/(period+1.0);seed=sum(values[:period])/period;out[period-1]=seed;cur=seed
    for i in range(period,len(values)):
        cur=alpha*values[i]+(1-alpha)*cur;out[i]=cur
    return out

def atrs(bars:list[Bar],period:int)->list[float|None]:
    out:[float|None]=[None]*len(bars)
    if len(bars)<period+1:return out
    trs=[None]
    for i in range(1,len(bars)):
        c,p=bars[i],bars[i-1];trs.append(max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)))
    for i in range(period,len(bars)):
        vals=[x for x in trs[i-period+1:i+1] if isinstance(x,(int,float))]
        if len(vals)==period:out[i]=sum(vals)/period
    return out

def precompute(symbol:str,bars:list[Bar])->SymbolData:
    closes=[b.close for b in bars];e=ema(closes,168);a=atrs(bars,protocol.ATR_PERIOD)
    comp=[False]*len(bars);ph=[None]*len(bars);tm=[None]*len(bars)
    sorted_window:list[float]=[]
    for i in range(len(bars)):
        if i>=168:
            old=a[i-168]
            if isinstance(old,(int,float)):
                j=bisect.bisect_left(sorted_window,float(old))
                if j<len(sorted_window):sorted_window.pop(j)
        cur=a[i]
        if isinstance(cur,(int,float)):
            if len(sorted_window)>=100:
                rank=bisect.bisect_right(sorted_window,float(cur))/len(sorted_window);comp[i]=rank<=0.30
            bisect.insort(sorted_window,float(cur))
        if i>=24:
            prev=bars[i-24:i];ph[i]=max(x.high for x in prev);tm[i]=statistics.median(x.turnover for x in prev)
    return SymbolData(symbol,bars,{b.ts:i for i,b in enumerate(bars)},e,a,comp,ph,tm)

def ret(sd:SymbolData,i:int,hours:int)->float|None:
    if i<hours:return None
    if sd.bars[i-hours].ts!=sd.bars[i].ts-hours*HOUR_MS:return None
    old=sd.bars[i-hours].close
    return sd.bars[i].close/old-1.0 if old>0 else None

def signal_score(sd:SymbolData,i:int,fam:dict[str,Any])->float|None:
    b=sd.bars[i];fid=fam["id"]
    if i+1>=len(sd.bars) or sd.bars[i+1].ts!=b.ts+HOUR_MS:return None
    r7=ret(sd,i,168)
    if r7 is None:return None
    if fid=="CROSS_SECTIONAL_MOMENTUM_V2":
        if ((b.ts//HOUR_MS)+1)%24!=0:return None
        r30=ret(sd,i,720)
        ev=sd.ema168[i]
        if r30 is None or ev is None or r7<fam["r7_min"] or r30<fam["r30_min"] or b.close<=ev:return None
        return r7+0.50*r30
    if fid=="TREND_PULLBACK_RESUMPTION_V2":
        if ((b.ts//HOUR_MS)+1)%4!=0:return None
        r24=ret(sd,i,24);ev=sd.ema168[i]
        if r24 is None or ev is None:return None
        if r7<fam["r7_min"] or not(fam["r24_min"]<=r24<=fam["r24_max"]) or b.close<=ev:return None
        if fam.get("require_bullish_signal") and not b.close>b.open:return None
        return r7-abs(r24)
    if fid=="VOLATILITY_COMPRESSION_BREAKOUT_V2":
        av=sd.atr14[i];prevh=sd.prev24_high[i];med=sd.prev24_turnover_median[i]
        if av is None or prevh is None or med is None or av<=0 or med<=0:return None
        if r7<fam["r7_min"] or not sd.compression[i] or b.close<=prevh or b.turnover<fam["turnover_multiple_min"]*med:return None
        return (b.close-prevh)/av
    return None

def execute(sd:SymbolData,i:int,fam:dict[str,Any],score:float,end_ms:int)->Trade|None:
    a=sd.atr14[i]
    if a is None or a<=0:return None
    entry_i=i+1;entry_bar=sd.bars[entry_i];entry=entry_bar.open
    mult=float(fam["atr_stop_multiple"]);rr=float(fam["risk_reward"]);hold=int(fam["max_hold_hours"])
    stop=entry-mult*a
    if entry<=0 or stop<=0 or stop>=entry:return None
    risk_unit=entry-stop;qty=protocol.RISK_AMOUNT_USD/risk_unit
    if qty*entry>protocol.REFERENCE_CAPITAL_USD*protocol.MAX_LEVERAGE+1e-9:return None
    target=entry+rr*risk_unit
    max_i=min(len(sd.bars)-1,entry_i+hold-1)
    if sd.bars[max_i].ts+HOUR_MS-1>end_ms:return None
    exit_price=None;reason=None;exit_i=None
    for j in range(entry_i,max_i+1):
        c=sd.bars[j]
        if c.low<=stop:exit_price=stop;reason="STOP_LOSS";exit_i=j;break
        if c.high>=target:exit_price=target;reason="TAKE_PROFIT";exit_i=j;break
    if exit_price is None:exit_i=max_i;exit_price=sd.bars[exit_i].close;reason="TIME_STOP"
    costs=compute_trade_costs(entry_price=entry,exit_price=float(exit_price),quantity=qty,side="LONG",config=TradingCostConfig())
    net=float(costs["net_pnl"]);closed=sd.bars[exit_i].ts+HOUR_MS
    return Trade(fam["id"],sd.symbol,sd.bars[i].ts+HOUR_MS-1,entry_bar.ts,closed,entry,stop,target,float(exit_price),str(reason),qty,net,net/protocol.RISK_AMOUNT_USD,score)

def simulate(data:dict[str,SymbolData],fam:dict[str,Any],start_ms:int,end_ms:int)->list[Trade]:
    times=sorted({b.ts for sd in data.values() for b in sd.bars if start_ms<=b.ts<=end_ms})
    busy_until=0;out=[]
    for ts in times:
        if ts+HOUR_MS<busy_until:continue
        candidates=[]
        for symbol,sd in data.items():
            i=sd.index.get(ts)
            if i is None:continue
            score=signal_score(sd,i,fam)
            if score is not None:candidates.append((float(score),symbol,i))
        if not candidates:continue
        candidates.sort(key=lambda x:(-x[0],x[1]))
        for score,symbol,i in candidates:
            trade=execute(data[symbol],i,fam,score,end_ms)
            if trade is not None:
                out.append(trade);busy_until=trade.closed_ms;break
    return out

def prefinal_eval(trades:list[Trade],fam:dict[str,Any],sample_id:str)->dict[str,Any]:
    ct=[t.closed_trade() for t in trades]
    val=validate_candidate(fam["id"],ct,sample_id=sample_id,holdout_fraction=0.25,window_count=4,safety_violations=())
    generic=promotion_gates(val)
    checks={
        "generic_pass":bool(generic.get("passed")),
        "min_oos_trades":int(val.get("oos_trades") or 0)>=protocol.PREFINAL_MIN_OOS_TRADES,
        "min_pf":isinstance(val.get("oos_profit_factor"),(int,float)) and float(val["oos_profit_factor"])>=protocol.PREFINAL_MIN_PF,
        "min_expectancy":isinstance(val.get("oos_expectancy_r"),(int,float)) and float(val["oos_expectancy_r"])>=protocol.PREFINAL_MIN_EXPECTANCY_R,
        "max_dd":isinstance(val.get("oos_max_drawdown_r"),(int,float)) and float(val["oos_max_drawdown_r"])<=protocol.PREFINAL_MAX_DD_R,
        "robustness":isinstance(val.get("robustness_ratio"),(int,float)) and float(val["robustness_ratio"])>=protocol.PREFINAL_MIN_ROBUSTNESS,
    }
    return {"family":fam,"trades":len(trades),"validation":val,"generic_gates":generic,"extra_checks":checks,"passed":all(checks.values())}

def final_eval(trades:list[Trade])->dict[str,Any]:
    cts=[t.closed_trade() for t in trades];stats=build_stats(cts)
    by={}
    for t in trades:by.setdefault(t.symbol,[]).append(t.closed_trade())
    represented=len(by);evaluable=0;profitable=0;per={}
    for s,rows in sorted(by.items()):
        st=build_stats(rows);ok=len(rows)>=protocol.FINAL_MIN_TRADES_PER_SYMBOL
        if ok:evaluable+=1;profitable+=int((st.get("net_pnl") or 0)>0 and (st.get("expectancy_r") or -999)>0)
        per[s]={"trades":len(rows),"stats":st,"evaluable":ok}
    ratio=profitable/evaluable if evaluable else 0.0
    checks={
        "min_trades":len(trades)>=protocol.FINAL_MIN_TRADES,
        "min_pf":isinstance(stats.get("profit_factor"),(int,float)) and float(stats["profit_factor"])>=protocol.FINAL_MIN_PF,
        "min_expectancy":isinstance(stats.get("expectancy_r"),(int,float)) and float(stats["expectancy_r"])>=protocol.FINAL_MIN_EXPECTANCY_R,
        "max_dd":isinstance(stats.get("max_drawdown_r"),(int,float)) and float(stats["max_drawdown_r"])<=protocol.FINAL_MAX_DD_R,
        "represented_symbols":represented>=protocol.FINAL_MIN_REPRESENTED_SYMBOLS,
        "evaluable_symbols":evaluable>=protocol.FINAL_MIN_EVALUABLE_SYMBOLS,
        "profitable_symbol_ratio":ratio>=protocol.FINAL_MIN_PROFITABLE_SYMBOL_RATIO,
    }
    return {"trades":len(trades),"stats":stats,"represented_symbols":represented,"evaluable_symbols":evaluable,"profitable_symbols":profitable,"profitable_symbol_ratio":round(ratio,4),"per_symbol":per,"checks":checks,"passed":all(checks.values()),"trade_details":[t.to_dict() for t in trades]}

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--state-dir",default="C:/TradingCore_Historical_Accelerator_V2");ap.add_argument("--v1-state",default="C:/TradingCore_Historical_Accelerator");ap.add_argument("--universe-lock",default="C:/TradingCore_Collector_C/data/UNIVERSE_LOCK.json");args=ap.parse_args()
    safety=assert_safe_startup();state=Path(args.state_dir);v1=Path(args.v1_state);state.mkdir(parents=True,exist_ok=True)
    lock=read_json(Path(args.universe_lock));sample=read_json(v1/"SAMPLE_LOCK.json")
    if not lock or not sample:raise SystemExit("Frozen universe or V1 price sample lock missing")
    symbols=[str(x).upper() for x in lock.get("symbols") or []];start=int(sample["start_ms"]);end=int(sample["end_ms"]);ufp=str(lock.get("fingerprint"))
    decision_path=state/"HISTORICAL_V2_DECISION_LOCK.json";existing=read_json(decision_path)
    if existing and existing.get("protocol_fingerprint")==protocol.PROTOCOL_FINGERPRINT and existing.get("universe_fingerprint")==ufp:
        print("="*92);print("HISTORICAL ACCELERATOR V2 — DECISION ALREADY LOCKED");print("State:",existing.get("state"));print("Candidate:",existing.get("candidate_family"));print("No final holdout re-opened.");print("="*92);return 0
    sample_lock={"schema":"TRADINGCORE_HISTORICAL_V2_SAMPLE_LOCK","locked_at_utc":datetime.now(timezone.utc).isoformat(),"protocol_version":protocol.PROTOCOL_VERSION,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":ufp,"symbols":symbols,"start_ms":start,"end_ms":end,"source_price_cache":str(v1/"cache"),"real_orders_enabled":False,"live_permission":False}
    atomic(state/"SAMPLE_LOCK.json",sample_lock)
    data={};coverage={}
    for s in symbols:
        bars=cache_bars(v1/"cache"/f"{s}.json.gz")
        if len(bars)<24*180:bars=fetch_bars(s,start,end)
        span=(bars[-1].ts-bars[0].ts)/DAY_MS if len(bars)>=2 else 0.0
        coverage[s]={"bars":len(bars),"span_days":round(span,2),"adequate":len(bars)>=24*180 and span>=180}
        if coverage[s]["adequate"]:data[s]=precompute(s,bars)
        print(f"{s}: bars={len(bars)} span_days={span:.1f} adequate={coverage[s]['adequate']}",flush=True)
    if len(data)<10:
        result={"state":"HISTORICAL_V2_DATA_INSUFFICIENT","adequate_symbols":len(data),"coverage":coverage,"real_orders_enabled":False,"live_permission":False};atomic(decision_path,{**result,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":ufp});atomic(state/"LATEST_HISTORICAL_V2.json",result);return 0
    final_cutoff=start+int((end-start)*protocol.PRE_FINAL_FRACTION);final_cutoff=(final_cutoff//HOUR_MS)*HOUR_MS
    pre_end=final_cutoff-1;prefinal=[];selected=None
    for fam in protocol.FAMILIES:
        trades=simulate(data,fam,start,pre_end);ev=prefinal_eval(trades,fam,f"{protocol.PROTOCOL_VERSION}:{protocol.PROTOCOL_FINGERPRINT[:12]}:{ufp[:12]}:{fam['id']}:PREFINAL")
        prefinal.append(ev)
        v=ev["validation"];print(f"PREFINAL {fam['id']}: pass={ev['passed']} trades={len(trades)} OOS={v.get('oos_trades')} PF={v.get('oos_profit_factor')} expR={v.get('oos_expectancy_r')} DD={v.get('oos_max_drawdown_r')} robust={v.get('robustness_ratio')}",flush=True)
        if selected is None and ev["passed"]:selected=fam
    final=None;candidate=None
    if selected is not None:
        print(f"Opening SEALED final 20% ONLY for selected family: {selected['id']}",flush=True)
        final_trades=simulate(data,selected,final_cutoff,end);final=final_eval(final_trades)
        candidate=selected["id"] if final["passed"] else None
        print(f"FINAL {selected['id']}: pass={final['passed']} trades={final['trades']} PF={final['stats'].get('profit_factor')} expR={final['stats'].get('expectancy_r')} DD={final['stats'].get('max_drawdown_r')} symbols={final['profitable_symbols']}/{final['evaluable_symbols']}",flush=True)
    final_state="HISTORICAL_V2_CANDIDATE_FOUND" if candidate else ("NO_PREFINAL_FAMILY_V2" if selected is None else "HISTORICAL_V2_FINAL_REJECTED")
    report={"schema":SCHEMA,"generated_at_utc":datetime.now(timezone.utc).isoformat(),"state":final_state,"protocol":{**protocol.protocol_dict(),"fingerprint":protocol.PROTOCOL_FINGERPRINT},"sample_lock":sample_lock,"final_cutoff_ms":final_cutoff,"final_cutoff_utc":utc(final_cutoff),"coverage":coverage,"adequate_symbols":len(data),"prefinal_results":prefinal,"selected_family_before_final":selected["id"] if selected else None,"final_result":final,"candidate_family":candidate,"safety":safety,"private_api_used":False,"real_orders_enabled":False,"live_permission":False,"collector_a_modified":False,"collector_b_modified":False,"collector_c_modified":False}
    atomic(state/"LATEST_HISTORICAL_V2.json",report)
    decision={"schema":"TRADINGCORE_HISTORICAL_V2_DECISION_LOCK","locked_at_utc":datetime.now(timezone.utc).isoformat(),"state":final_state,"protocol_version":protocol.PROTOCOL_VERSION,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":ufp,"selected_family_before_final":report["selected_family_before_final"],"candidate_family":candidate,"final_result_summary":None if final is None else {k:final.get(k) for k in ("trades","represented_symbols","evaluable_symbols","profitable_symbols","profitable_symbol_ratio","checks","passed")},"holdout_reopen_allowed":False,"real_orders_enabled":False,"live_permission":False}
    atomic(decision_path,decision)
    if candidate:
        atomic(state/"CANDIDATE_FOR_FORWARD_PAPER.json",{"schema":"TRADINGCORE_HISTORICAL_V2_FORWARD_CANDIDATE","authorized_at_utc":datetime.now(timezone.utc).isoformat(),"mode":"PAPER_ONLY","protocol_version":protocol.PROTOCOL_VERSION,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"universe_fingerprint":ufp,"family":selected,"historical_final":final,"real_orders_enabled":False,"live_permission":False})
    print("\n"+"="*92);print("HISTORICAL ACCELERATOR V2 FINAL RESULT");print("State:",final_state);print("Selected before final:",report["selected_family_before_final"]);print("Candidate:",candidate);print("Adequate symbols:",len(data),"/",len(symbols));
    if final:print("Final trades:",final["trades"],"PF=",final["stats"].get("profit_factor"),"expR=",final["stats"].get("expectancy_r"),"DD=",final["stats"].get("max_drawdown_r"),"symbols=",f"{final['profitable_symbols']}/{final['evaluable_symbols']}")
    print("LIVE / real orders: DISABLED");print("Report:",state/"LATEST_HISTORICAL_V2.json");print("Decision:",decision_path);print("="*92)
    return 0

if __name__=="__main__":raise SystemExit(main())
