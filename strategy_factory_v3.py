#!/usr/bin/env python3
"""TradingCore bounded Strategy Factory V3.

Selection: EARLY half of cached Bybit 1H spot history only.
Final: LATER half fetched from OKX public spot only AFTER the winner is frozen.
No authenticated APIs, no order clients, no LIVE path.
"""
from __future__ import annotations

import argparse, gzip, itertools, json, math, statistics, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from api.strategy_supervisor.validation import build_walk_forward_windows, robustness_ratio
from config.startup_safety import assert_safe_startup
import strategy_factory_v3_protocol as protocol

HOUR_MS=3_600_000
DAY_MS=86_400_000
OKX="https://www.okx.com"
SCHEMA="TRADINGCORE_STRATEGY_FACTORY_V3"

@dataclass(frozen=True)
class Bar:
 ts:int; open:float; high:float; low:float; close:float; volume:float

@dataclass(frozen=True)
class Signal:
 family_id:str; symbol:str; signal_ts:int; entry_ts:int; entry:float; stop:float; target:float; score:float; entry_index:int


def utc(ms:int)->str:return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()
def now()->str:return datetime.now(timezone.utc).isoformat()
def atomic(path:Path,payload:dict[str,Any])->None:
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+".tmp");tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False,default=str),encoding="utf-8");tmp.replace(path)

def load_cache(symbol:str)->list[Bar]:
 path=Path(f"C:/TradingCore_Historical_Accelerator/cache/{symbol}.json.gz")
 if not path.exists():return []
 try:
  with gzip.open(path,"rt",encoding="utf-8") as h:p=json.load(h)
  out=[]
  for r in (p.get("bars") if isinstance(p,dict) else []) or []:
   try:out.append(Bar(int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])))
   except (TypeError,ValueError,IndexError):pass
  return sorted({b.ts:b for b in out}.values(),key=lambda b:b.ts)
 except Exception:return []

def okx_json(path:str,params:dict[str,Any])->dict[str,Any]:
 q=urlencode({k:v for k,v in params.items() if v is not None and v!=""})
 req=Request(f"{OKX}{path}?{q}",headers={"Accept":"application/json","User-Agent":"TradingCore-FactoryV3/1.0"})
 last=None
 for attempt in range(6):
  try:
   with urlopen(req,timeout=25) as h:p=json.loads(h.read().decode())
   if not isinstance(p,dict) or str(p.get("code"))!="0":raise RuntimeError(f"OKX response {p}")
   return p
  except Exception as e:
   last=e
   if attempt==5:break
   time.sleep(min(8,0.5*(2**attempt)))
 raise RuntimeError(f"OKX public request failed: {last}")

def fetch_okx(symbol:str,start_ms:int,end_ms:int)->list[Bar]:
 inst=symbol[:-4]+"-USDT" if symbol.endswith("USDT") else symbol
 by:dict[int,Bar]={};after=None
 for _ in range(80):
  p=okx_json("/api/v5/market/history-candles",{"instId":inst,"bar":"1H","after":after,"limit":300})
  rows=p.get("data") or []
  if not rows:break
  oldest=None
  for r in rows:
   if not isinstance(r,list) or len(r)<6:continue
   try:
    ts=int(r[0]);confirm=str(r[8]) if len(r)>8 else "1"
    b=Bar(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]))
   except (TypeError,ValueError,IndexError):continue
   oldest=ts if oldest is None else min(oldest,ts)
   if start_ms<=ts<=end_ms and confirm=="1":by[ts]=b
  if oldest is None or oldest<=start_ms:break
  after=str(oldest-1);time.sleep(0.12)
 return sorted(by.values(),key=lambda b:b.ts)

def ema(values:list[float],period:int)->list[float|None]:
 out:[float|None]=[None]*len(values)
 if period<=0 or len(values)<period:return out
 cur=sum(values[:period])/period;out[period-1]=cur;a=2/(period+1)
 for i in range(period,len(values)):
  cur=a*values[i]+(1-a)*cur;out[i]=cur
 return out

def atr(bars:list[Bar],period:int)->list[float|None]:
 out:[float|None]=[None]*len(bars);trs:[float|None]=[None]
 for i in range(1,len(bars)):
  c,p=bars[i],bars[i-1];trs.append(max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)))
 for i in range(period,len(bars)):
  x=[v for v in trs[i-period+1:i+1] if isinstance(v,(int,float))]
  if len(x)==period:out[i]=sum(x)/period
 return out

def rsi(values:list[float],period:int)->list[float|None]:
 out:[float|None]=[None]*len(values)
 if len(values)<=period:return out
 gains=[];losses=[]
 for i in range(1,period+1):
  d=values[i]-values[i-1];gains.append(max(d,0));losses.append(max(-d,0))
 ag=sum(gains)/period;al=sum(losses)/period
 out[period]=100.0 if al==0 else 100-100/(1+ag/al)
 for i in range(period+1,len(values)):
  d=values[i]-values[i-1];g=max(d,0);l=max(-d,0);ag=(ag*(period-1)+g)/period;al=(al*(period-1)+l)/period
  out[i]=100.0 if al==0 else 100-100/(1+ag/al)
 return out

def contiguous(bars:list[Bar],i:int,h:int)->bool:return i>=h and bars[i-h].ts==bars[i].ts-h*HOUR_MS

def parameter_sets()->list[dict[str,Any]]:
 out=[]
 for fam in protocol.FAMILIES:
  fid=fam["id"]
  keys=[k for k in fam if k!="id"]
  vals=[fam[k] if isinstance(fam[k],list) else [fam[k]] for k in keys]
  for combo in itertools.product(*vals):out.append({"id":fid,**dict(zip(keys,combo))})
 return out

def signals_for(symbol:str,bars:list[Bar],cfg:dict[str,Any])->list[Signal]:
 if len(bars)<300:return []
 closes=[b.close for b in bars];av=atr(bars,protocol.ATR_PERIOD);fid=cfg["id"]
 periods=set()
 for key in ("fast_ema","slow_ema","trend_filter_ema"):
  if key in cfg:periods.add(int(cfg[key]))
 emas={p:ema(closes,p) for p in periods};rsis={}
 if "rsi_period" in cfg:rsis[int(cfg["rsi_period"])]=rsi(closes,int(cfg["rsi_period"]))
 out=[]
 for i in range(250,len(bars)-1):
  b=bars[i];nxt=bars[i+1]
  if nxt.ts!=b.ts+HOUR_MS:continue
  a=av[i]
  if not isinstance(a,(int,float)) or a<=0:continue
  score=None
  if fid=="DONCHIAN_TREND_BREAKOUT_V3":
   f=emas[int(cfg["fast_ema"])][i];s=emas[int(cfg["slow_ema"])][i];lb=int(cfg["breakout_hours"])
   if f is None or s is None or i<lb or f<=s:continue
   prev=max(x.high for x in bars[i-lb:i])
   if b.close<=prev:continue
   score=(b.close/prev-1)+(f/s-1)
  elif fid=="TREND_PULLBACK_RECOVERY_V3":
   f=emas[int(cfg["fast_ema"])][i];s=emas[int(cfg["slow_ema"])][i];lb=int(cfg["lookback_hours"]);pa=float(cfg["pullback_atr"])
   if f is None or s is None or f<=s or i<lb or not b.close>b.open or b.close<=f:continue
   touched=False
   for j in range(i-lb,i):
    fj=emas[int(cfg["fast_ema"])][j];aj=av[j]
    if fj is not None and aj is not None and bars[j].low<=fj+pa*aj:touched=True;break
   if not touched:continue
   score=(f/s-1)+max(0,(b.close-f)/b.close)
  elif fid=="PANIC_MEAN_REVERSION_V3":
   rp=int(cfg["rsi_period"]);rv=rsis[rp][i];te=emas[int(cfg["trend_filter_ema"])][i]
   if rv is None or te is None or not contiguous(bars,i,24):continue
   ret24=b.close/bars[i-24].close-1
   if rv>float(cfg["rsi_lte"]) or ret24>float(cfg["drop_24h_lte"]) or b.close<te or not b.close>b.open:continue
   score=(-ret24)+(50-rv)/100
  if score is None:continue
  entry=nxt.open;stop=entry-protocol.ATR_STOP_MULTIPLE*a
  if entry<=0 or stop<=0 or stop>=entry:continue
  risk=entry-stop;target=entry+protocol.TARGET_R*risk
  if (protocol.RISK_AMOUNT_USD/risk)*entry>protocol.REFERENCE_CAPITAL_USD*protocol.MAX_LEVERAGE+1e-9:continue
  out.append(Signal(fid,symbol,b.ts,nxt.ts,entry,stop,target,float(score),i+1))
 return out

def simulate_portfolio(data:dict[str,list[Bar]],cfg:dict[str,Any])->tuple[list[ClosedTrade],dict[str,int]]:
 allsig=[];index={}
 for sym,bars in data.items():
  index[sym]={b.ts:i for i,b in enumerate(bars)};allsig.extend(signals_for(sym,bars,cfg))
 # At the same timestamp choose strongest signal only; one position globally.
 grouped:dict[int,list[Signal]]={}
 for s in allsig:grouped.setdefault(s.entry_ts,[]).append(s)
 chosen=[max(v,key=lambda x:(x.score,x.symbol)) for _,v in sorted(grouped.items())]
 costs=TradingCostConfig();trades=[];per={};busy_until=-1
 for s in chosen:
  if s.entry_ts<=busy_until:continue
  bars=data[s.symbol];i=index[s.symbol].get(s.entry_ts)
  if i is None:continue
  exit_px=None;exit_i=None
  end=min(len(bars)-1,i+protocol.MAX_HOLD_HOURS)
  for j in range(i,end+1):
   b=bars[j]
   if b.low<=s.stop:exit_px=s.stop;exit_i=j;break
   if b.high>=s.target:exit_px=s.target;exit_i=j;break
  if exit_px is None:exit_i=end;exit_px=bars[end].close
  qty=protocol.RISK_AMOUNT_USD/(s.entry-s.stop)
  res=compute_trade_costs(entry_price=s.entry,exit_price=exit_px,quantity=qty,side="LONG",config=costs)
  trades.append(ClosedTrade(s.family_id,utc(bars[exit_i].ts),s.symbol,float(res["net_pnl"]),float(res["net_pnl"])/protocol.RISK_AMOUNT_USD))
  per[s.symbol]=per.get(s.symbol,0)+1;busy_until=bars[exit_i].ts
 return trades,per

def robust(trades:list[ClosedTrade],windows:int=4)->float|None:
 return robustness_ratio(build_walk_forward_windows(trades,window_count=windows))

def metrics(trades:list[ClosedTrade])->dict[str,Any]:
 st=build_stats(trades);st["robustness_ratio"]=robust(trades);return st

def dev_pass(st:dict[str,Any])->bool:
 pf=st.get("profit_factor");ex=st.get("expectancy_r");dd=st.get("max_drawdown_r");rr=st.get("robustness_ratio")
 return bool((st.get("closed_trades") or 0)>=protocol.DEV_MIN_TRADES and isinstance(pf,(int,float)) and pf>=protocol.DEV_MIN_PF and isinstance(ex,(int,float)) and ex>=protocol.DEV_MIN_EXPECTANCY_R and isinstance(dd,(int,float)) and dd<=protocol.DEV_MAX_DD_R and isinstance(rr,(int,float)) and rr>=protocol.DEV_MIN_ROBUSTNESS)

def final_pass(trades:list[ClosedTrade],st:dict[str,Any])->tuple[bool,dict[str,Any]]:
 per={}
 for t in trades:per.setdefault(str(t.regime),[]).append(t)
 pstats={s:build_stats(v) for s,v in per.items()};prof=sum(1 for x in pstats.values() if isinstance(x.get("net_pnl"),(int,float)) and x["net_pnl"]>0)
 pf=st.get("profit_factor");ex=st.get("expectancy_r");dd=st.get("max_drawdown_r");rr=st.get("robustness_ratio")
 checks={
  "min_trades":(st.get("closed_trades") or 0)>=protocol.FINAL_MIN_TRADES,
  "pf":isinstance(pf,(int,float)) and pf>=protocol.FINAL_MIN_PF,
  "expectancy":isinstance(ex,(int,float)) and ex>=protocol.FINAL_MIN_EXPECTANCY_R,
  "drawdown":isinstance(dd,(int,float)) and dd<=protocol.FINAL_MAX_DD_R,
  "profitable_symbols":prof>=protocol.FINAL_MIN_PROFITABLE_SYMBOLS,
  "robustness":isinstance(rr,(int,float)) and rr>=protocol.FINAL_MIN_ROBUSTNESS,
 }
 return all(checks.values()),{"checks":checks,"profitable_symbols":prof,"per_symbol":pstats}

def main()->int:
 ap=argparse.ArgumentParser();ap.add_argument("--state-dir",default="C:/TradingCore_Strategy_Factory_V3");args=ap.parse_args();safety=assert_safe_startup();state=Path(args.state_dir);state.mkdir(parents=True,exist_ok=True)
 decision=state/"DECISION_LOCK.json"
 if decision.exists():
  d=json.loads(decision.read_text(encoding="utf-8-sig"));print("="*92);print("STRATEGY FACTORY V3 — DECISION ALREADY LOCKED");print("State:",d.get("state"));print("Candidate:",d.get("candidate"));print("No final holdout re-opened.");print("="*92);return 0
 data={s:load_cache(s) for s in protocol.SYMBOLS};data={s:b for s,b in data.items() if len(b)>=24*500}
 if len(data)<3:raise SystemExit("Need adequate cached BTC/ETH/SOL Bybit history")
 common_start=max(v[0].ts for v in data.values());common_end=min(v[-1].ts for v in data.values());boundary=common_start+(common_end-common_start)//2;boundary=(boundary//HOUR_MS)*HOUR_MS
 dev={s:[b for b in bars if b.ts<boundary] for s,bars in data.items()}
 print("="*92);print("TRADINGCORE STRATEGY FACTORY V3");print("Protocol:",protocol.PROTOCOL_VERSION,protocol.PROTOCOL_FINGERPRINT);print("DEV Bybit:",utc(common_start),"->",utc(boundary-1));print("FINAL OKX (sealed until winner frozen):",utc(boundary),"->",utc(common_end));print("No API keys | No orders | LIVE disabled");print("="*92,flush=True)
 results=[]
 for cfg in parameter_sets():
  trades,_=simulate_portfolio(dev,cfg);st=metrics(trades);passed=dev_pass(st)
  results.append({"config":cfg,"stats":st,"passed":passed})
  print(f"DEV {cfg}: trades={st.get('closed_trades')} PF={st.get('profit_factor')} expR={st.get('expectancy_r')} DD={st.get('max_drawdown_r')} robust={st.get('robustness_ratio')} pass={passed}",flush=True)
 passed=[r for r in results if r["passed"]]
 if not passed:
  final_state="NO_DEVELOPMENT_CANDIDATE_V3";candidate=None;final_stats=None;final_detail=None
 else:
  passed.sort(key=lambda r:(float(r["stats"].get("expectancy_r") or -999),float(r["stats"].get("profit_factor") or -999),float(r["stats"].get("robustness_ratio") or -999),int(r["stats"].get("closed_trades") or 0)),reverse=True)
  winner=passed[0];candidate=winner["config"]
  freeze={"schema":"TRADINGCORE_FACTORY_V3_SELECTION_LOCK","locked_at_utc":now(),"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"candidate":candidate,"development_stats":winner["stats"],"boundary_ms":boundary,"final_not_yet_evaluated":True,"real_orders_enabled":False}
  atomic(state/"SELECTION_LOCK_BEFORE_FINAL.json",freeze)
  print("FROZEN WINNER BEFORE FINAL:",candidate,flush=True)
  # Only now fetch the later half from OKX.
  final_data={}
  for sym in protocol.SYMBOLS:
   rows=fetch_okx(sym,boundary,common_end);final_data[sym]=rows;print(f"FINAL DATA {sym}: bars={len(rows)}",flush=True)
  if any(len(final_data[s])<24*300 for s in protocol.SYMBOLS):
   final_state="FINAL_DATA_INSUFFICIENT_V3";final_stats=None;final_detail=None
  else:
   ft,_=simulate_portfolio(final_data,candidate);final_stats=metrics(ft);ok,final_detail=final_pass(ft,final_stats);final_state="STRATEGY_FACTORY_V3_CANDIDATE_FOUND" if ok else "FINAL_CANDIDATE_REJECTED_V3"
   if ok:
    atomic(state/"CANDIDATE_FOR_FORWARD_PAPER.json",{"schema":"TRADINGCORE_FACTORY_V3_FORWARD_CANDIDATE","authorized_at_utc":now(),"mode":"PAPER_ONLY","protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"candidate":candidate,"development_stats":winner["stats"],"final_stats":final_stats,"final_detail":final_detail,"real_orders_enabled":False,"live_permission":False})
 report={"schema":SCHEMA,"generated_at_utc":now(),"state":final_state,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"boundary_utc":utc(boundary),"development_results":results,"candidate":candidate,"final_stats":final_stats,"final_detail":final_detail,"safety":safety,"real_orders_enabled":False,"live_permission":False}
 atomic(state/"LATEST_STRATEGY_FACTORY_V3.json",report);atomic(decision,{"schema":"TRADINGCORE_FACTORY_V3_DECISION_LOCK","locked_at_utc":now(),"state":final_state,"candidate":candidate,"protocol_fingerprint":protocol.PROTOCOL_FINGERPRINT,"holdout_reopen_allowed":False,"real_orders_enabled":False,"live_permission":False})
 print("\n"+"="*92);print("STRATEGY FACTORY V3 FINAL RESULT");print("State:",final_state);print("Candidate:",candidate);print("Final stats:",final_stats);print("Final detail:",final_detail);print("LIVE / real orders: DISABLED");print("Report:",state/"LATEST_STRATEGY_FACTORY_V3.json");print("="*92)
 return 0
if __name__=="__main__":raise SystemExit(main())
