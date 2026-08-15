#!/usr/bin/env python3
"""Strategy Atlas relative-value/stat-arb screen on Binance + OKX 1H data.
Research/PAPER only. Pair shorts are simulated only for research; no order path.
"""
from __future__ import annotations
import argparse,json,math,statistics
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs
from config.startup_safety import assert_safe_startup
from strategy_atlas_price_v2 import binance,okx
from strategy_atlas_protocol import PROTOCOL_FINGERPRINT,PROTOCOL_VERSION,RELATIVE_VALUE_FAMILIES,SPOT_SYMBOLS

PAIRS=(("BTCUSDT","ETHUSDT"),("SOLUSDT","ETHUSDT"),("BCHUSDT","LTCUSDT"))
CAPITAL=1000.0

def atomic(p:Path,d:dict):
 p.parent.mkdir(parents=True,exist_ok=True);q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(d,indent=2,default=str),encoding='utf-8');q.replace(p)
def fin(x):return isinstance(x,(int,float)) and math.isfinite(float(x))
def stats(returns):
 if not returns:return {'trades':0,'net_return_pct':None,'profit_factor':None,'avg_return_bps':None,'max_drawdown_pct':None}
 wins=sum(x for x in returns if x>0);loss=-sum(x for x in returns if x<0);eq=peak=dd=0.0
 for x in returns:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
 return {'trades':len(returns),'net_return_pct':round(sum(returns)*100,4),'profit_factor':round(wins/loss,4) if loss>0 else None,'avg_return_bps':round(statistics.fmean(returns)*10000,4),'max_drawdown_pct':round(dd*100,4)}
def zscore(vals,n=60):
 out=[None]*len(vals)
 for i in range(n-1,len(vals)):
  w=vals[i-n+1:i+1];m=statistics.fmean(w);sd=statistics.pstdev(w);out[i]=0.0 if sd<=1e-12 else (vals[i]-m)/sd
 return out
def aligned(a,b):
 da={x.open_time_ms:x for x in a};db={x.open_time_ms:x for x in b};ts=sorted(set(da)&set(db));return [da[t] for t in ts],[db[t] for t in ts]
def pair_reversion(a,b,label):
 a,b=aligned(a,b)
 if len(a)<200:return {'family':label,'trades':[],'stats':stats([])}
 ratio=[math.log(x.close/y.close) for x,y in zip(a,b)];z=zscore(ratio,60);cost=TradingCostConfig();ret=[];pos=None
 for i in range(60,len(a)):
  if pos:
   exit_now=(pos['dir']==1 and z[i] is not None and z[i]>=0) or (pos['dir']==-1 and z[i] is not None and z[i]<=0) or (z[i] is not None and abs(z[i])>=3.5) or i-pos['i']>=48
   if exit_now:
    ca=compute_trade_costs(entry_price=pos['a'],exit_price=a[i].close,quantity=pos['qa'],side='LONG' if pos['dir']==1 else 'SHORT',config=cost)
    cb=compute_trade_costs(entry_price=pos['b'],exit_price=b[i].close,quantity=pos['qb'],side='SHORT' if pos['dir']==1 else 'LONG',config=cost)
    ret.append((ca['net_pnl']+cb['net_pnl'])/CAPITAL);pos=None
  if pos or z[i] is None:continue
  if z[i]<=-2.0:direction=1
  elif z[i]>=2.0:direction=-1
  else:continue
  pos={'dir':direction,'a':a[i].close,'b':b[i].close,'qa':(CAPITAL*.5)/a[i].close,'qb':(CAPITAL*.5)/b[i].close,'i':i}
 return {'family':label,'trades':ret,'stats':stats(ret)}
def pair_momentum(a,b,label):
 a,b=aligned(a,b);cost=TradingCostConfig();ret=[]
 for i in range(168,len(a)-24,24):
  ra=a[i].close/a[i-168].close-1;rb=b[i].close/b[i-168].close-1
  if abs(ra-rb)<.03:continue
  long_a=ra>rb;qa=(CAPITAL*.5)/a[i].close;qb=(CAPITAL*.5)/b[i].close
  ca=compute_trade_costs(entry_price=a[i].close,exit_price=a[i+24].close,quantity=qa,side='LONG' if long_a else 'SHORT',config=cost)
  cb=compute_trade_costs(entry_price=b[i].close,exit_price=b[i+24].close,quantity=qb,side='SHORT' if long_a else 'LONG',config=cost)
  ret.append((ca['net_pnl']+cb['net_pnl'])/CAPITAL)
 return {'family':label,'trades':ret,'stats':stats(ret)}
def cross_sectional(market,family):
 common=sorted(set.intersection(*[{x.open_time_ms for x in rows} for rows in market.values()])) if market else []
 maps={s:{x.open_time_ms:x for x in rows} for s,rows in market.items()};cost=TradingCostConfig();ret=[]
 for j in range(168,len(common)-24,24):
  t=common[j];past=common[j-168];future=common[j+24];scores=[]
  for s in market:
   if t in maps[s] and past in maps[s] and future in maps[s]:scores.append((maps[s][t].close/maps[s][past].close-1,s))
  if len(scores)<5:continue
  scores.sort();score,s=(scores[-1] if family=='CROSS_SECTIONAL_MOMENTUM_ROTATION' else scores[0])
  if family=='CROSS_SECTIONAL_MOMENTUM_ROTATION' and score<=0:continue
  q=CAPITAL/maps[s][t].close;cc=compute_trade_costs(entry_price=maps[s][t].close,exit_price=maps[s][future].close,quantity=q,side='LONG',config=cost);ret.append(cc['net_pnl']/CAPITAL)
 return {'family':family,'trades':ret,'stats':stats(ret)}
def pass_check(s):
 return int(s.get('trades') or 0)>=30 and fin(s.get('profit_factor')) and s['profit_factor']>=1.15 and fin(s.get('avg_return_bps')) and s['avg_return_bps']>0 and fin(s.get('max_drawdown_pct')) and s['max_drawdown_pct']<=10

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output',default='strategy_atlas_relative_runtime');args=ap.parse_args();safe=assert_safe_startup();out=Path(args.output);venue_results={};fail={}
 for venue,fn in [('BINANCE',binance),('OKX',okx)]:
  market={}
  for s in SPOT_SYMBOLS:
   try:market[s]=fn(s,'1h');print('REL_DATA',venue,s,len(market[s]),flush=True)
   except Exception as e:fail[f'{venue}:{s}']=f'{type(e).__name__}: {e}'
  rows=[]
  for a,b in PAIRS:
   if a in market and b in market:
    rows.append(pair_reversion(market[a],market[b],f'{a[:-4]}_{b[:-4]}_RATIO_MEAN_REVERSION'))
    rows.append(pair_momentum(market[a],market[b],f'{a[:-4]}_{b[:-4]}_RELATIVE_MOMENTUM'))
  if len(market)>=5:
   rows.append(cross_sectional(market,'CROSS_SECTIONAL_MOMENTUM_ROTATION'));rows.append(cross_sectional(market,'CROSS_SECTIONAL_SHORT_TERM_REVERSAL'))
  for r in rows:r['passed']=pass_check(r['stats'])
  venue_results[venue]=rows
 keys=set(r['family'] for rs in venue_results.values() for r in rs);passed=[];allrows=[]
 for k in sorted(keys):
  per={v:next((r for r in venue_results.get(v,[]) if r['family']==k),None) for v in ('BINANCE','OKX')};ok=all(per[v] and per[v]['passed'] for v in per);row={'family':k,'state':'ATLAS_RELATIVE_PASS_NOT_LIVE' if ok else 'ATLAS_RELATIVE_REJECT','venues':per};allrows.append(row);passed+=([row] if ok else [])
 report={'schema':'TRADINGCORE_STRATEGY_ATLAS_RELATIVE_V1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'state':'ATLAS_RELATIVE_CANDIDATE_FOUND_NOT_LIVE' if passed else 'NO_ATLAS_RELATIVE_CANDIDATE','protocol_version':PROTOCOL_VERSION,'protocol_fingerprint':PROTOCOL_FINGERPRINT,'families_declared':list(RELATIVE_VALUE_FAMILIES),'tested_mechanisms':sorted(keys),'passing':passed,'results':allrows,'data_failures':fail,'safety':safe,'real_orders_enabled':False,'live_permission':False}
 atomic(out/'STRATEGY_ATLAS_RELATIVE_RESULT.json',report);print('='*88);print('STRATEGY ATLAS RELATIVE FINAL');print('State:',report['state']);print('Passing:',[x['family'] for x in passed]);print('LIVE / real orders: DISABLED');print('='*88);return 0
if __name__=='__main__':raise SystemExit(main())
