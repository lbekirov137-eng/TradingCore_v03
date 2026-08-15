#!/usr/bin/env python3
"""Strategy Atlas derivatives/carry screen using public OKX spot+swap+funding.
Research/PAPER only. Simulated market-neutral legs; no authenticated API/orders.
"""
from __future__ import annotations
import argparse,bisect,json,math,statistics,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs
from api.strategy_engine.strategies.contracts import Candle
from config.startup_safety import assert_safe_startup
from strategy_atlas_protocol import DERIVATIVE_FAMILIES,PROTOCOL_FINGERPRINT,PROTOCOL_VERSION

OKX='https://www.okx.com';CAPITAL=1000.0;SYMS=('BTC','ETH','SOL');DAY=86_400_000

def req(url,attempts=6):
 last=None
 for n in range(attempts):
  try:
   with urlopen(Request(url,headers={'User-Agent':'TradingCore-DerivAtlas/1.0'}),timeout=25) as r:return json.loads(r.read().decode())
  except (HTTPError,URLError,TimeoutError,json.JSONDecodeError) as e:
   last=e
   if n+1<attempts:time.sleep(min(8,.5*(2**n)))
 raise RuntimeError(last)
def atomic(p,d):p.parent.mkdir(parents=True,exist_ok=True);q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(d,indent=2,default=str),encoding='utf-8');q.replace(p)
def candles(inst,days=180):
 now=int(time.time()*1000);start=now-days*DAY;cursor=None;by={}
 for _ in range(120):
  pars={'instId':inst,'bar':'4H','limit':100}
  if cursor is not None:pars['after']=str(cursor)
  d=req(f"{OKX}/api/v5/market/history-candles?{urlencode(pars)}")
  if str(d.get('code'))!='0':raise RuntimeError(d)
  rows=d.get('data') or []
  if not rows:break
  old=None
  for r in rows:
   try:
    ts=int(r[0]);old=ts if old is None else min(old,ts);conf=str(r[8])=='1' if len(r)>8 else True
    if conf and ts>=start:by[ts]=Candle(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]))
   except:pass
  if old is None or old<=start or (cursor is not None and old>=cursor):break
  cursor=old;time.sleep(.22)
 return sorted(by.values(),key=lambda x:x.open_time_ms)
def funding(inst,days=180):
 now=int(time.time()*1000);start=now-days*DAY;cursor=None;by={}
 for _ in range(80):
  pars={'instId':inst,'limit':100}
  if cursor is not None:pars['after']=str(cursor)
  d=req(f"{OKX}/api/v5/public/funding-rate-history?{urlencode(pars)}")
  if str(d.get('code'))!='0':raise RuntimeError(d)
  rows=d.get('data') or []
  if not rows:break
  old=None
  for r in rows:
   try:
    ts=int(r.get('fundingTime'));old=ts if old is None else min(old,ts)
    if ts>=start:by[ts]=float(r.get('realizedRate') or r.get('fundingRate'))
   except:pass
  if old is None or old<=start or (cursor is not None and old>=cursor):break
  cursor=old;time.sleep(.22)
 return sorted(by.items())
def align(a,b):
 da={x.open_time_ms:x for x in a};db={x.open_time_ms:x for x in b};ts=sorted(set(da)&set(db));return ts,da,db
def metrics(ret):
 if not ret:return {'trades':0,'net_return_pct':None,'profit_factor':None,'avg_return_bps':None,'max_drawdown_pct':None}
 w=sum(x for x in ret if x>0);l=-sum(x for x in ret if x<0);eq=peak=dd=0
 for x in ret:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
 return {'trades':len(ret),'net_return_pct':round(sum(ret)*100,4),'profit_factor':round(w/l,4) if l>0 else None,'avg_return_bps':round(statistics.fmean(ret)*10000,4),'max_drawdown_pct':round(dd*100,4)}
def pair_pnl(sa,sb,ea,eb,long_spot,notional=500):
 c=TradingCostConfig();qa=notional/sa;qb=notional/sb;pa=compute_trade_costs(entry_price=sa,exit_price=ea,quantity=qa,side='LONG' if long_spot else 'SHORT',config=c);pb=compute_trade_costs(entry_price=sb,exit_price=eb,quantity=qb,side='SHORT' if long_spot else 'LONG',config=c);return pa['net_pnl']+pb['net_pnl']
def simulate(symbol,spot,swap,fr):
 ts,ds,dw=align(spot,swap);fts=[x[0] for x in fr];fvs=[x[1] for x in fr];basis=[dw[t].close/ds[t].close-1 for t in ts];out={k:[] for k in ('POSITIVE_FUNDING_CARRY','NEGATIVE_FUNDING_REVERSE_CARRY','FUNDING_EXTREME_REVERSAL','PREMIUM_INDEX_CONVERGENCE','MARK_INDEX_DIVERGENCE')}
 for i in range(6,len(ts)-6):
  ft=ts[i]+4*3_600_000;j=bisect.bisect_right(fts,ft)-1
  rate=fvs[j] if j>=0 else None
  if rate is None:continue
  if rate>=.0001 and basis[i]>=0:
   pnl=pair_pnl(ds[ts[i]].close,dw[ts[i]].close,ds[ts[i+2]].close,dw[ts[i+2]].close,True)+500*rate;out['POSITIVE_FUNDING_CARRY'].append(pnl/CAPITAL)
  if rate<=-.0001 and basis[i]<=0:
   pnl=pair_pnl(ds[ts[i]].close,dw[ts[i]].close,ds[ts[i+2]].close,dw[ts[i+2]].close,False)+500*(-rate);out['NEGATIVE_FUNDING_REVERSE_CARRY'].append(pnl/CAPITAL)
  if abs(basis[i])>=.005:
   long_spot=basis[i]>0;pnl=pair_pnl(ds[ts[i]].close,dw[ts[i]].close,ds[ts[i+6]].close,dw[ts[i+6]].close,long_spot);out['PREMIUM_INDEX_CONVERGENCE'].append(pnl/CAPITAL);out['MARK_INDEX_DIVERGENCE'].append(pnl/CAPITAL)
  if rate<=-.0005 and ds[ts[i]].close/ds[ts[i-6]].close-1<=-.03:
   c=TradingCostConfig();entry=ds[ts[i]].close;exitp=ds[ts[i+6]].close;q=CAPITAL/entry;p=compute_trade_costs(entry_price=entry,exit_price=exitp,quantity=q,side='LONG',config=c);out['FUNDING_EXTREME_REVERSAL'].append(p['net_pnl']/CAPITAL)
 return {k:metrics(v) for k,v in out.items()}
def ok(s):return int(s.get('trades') or 0)>=20 and isinstance(s.get('profit_factor'),(int,float)) and s['profit_factor']>=1.15 and isinstance(s.get('avg_return_bps'),(int,float)) and s['avg_return_bps']>0 and isinstance(s.get('max_drawdown_pct'),(int,float)) and s['max_drawdown_pct']<=10

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output',default='strategy_atlas_derivatives_runtime');args=ap.parse_args();safe=assert_safe_startup();out=Path(args.output);res=[];fail={}
 for s in SYMS:
  try:
   sp=candles(f'{s}-USDT');sw=candles(f'{s}-USDT-SWAP');fr=funding(f'{s}-USDT-SWAP');r=simulate(s,sp,sw,fr)
   for fam,m in r.items():res.append({'symbol':s,'family':fam,'stats':m,'state':'ATLAS_DERIVATIVE_SHORTLIST_NOT_LIVE' if ok(m) else 'ATLAS_DERIVATIVE_REJECT'})
   print('DERIV_DATA',s,'spot',len(sp),'swap',len(sw),'funding',len(fr),flush=True)
  except Exception as e:fail[s]=f'{type(e).__name__}: {e}'
 passed=[x for x in res if x['state']=='ATLAS_DERIVATIVE_SHORTLIST_NOT_LIVE'];report={'schema':'TRADINGCORE_STRATEGY_ATLAS_DERIVATIVES_V1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'state':'ATLAS_DERIVATIVE_SHORTLIST_FOUND_NOT_LIVE' if passed else 'NO_ATLAS_DERIVATIVE_SHORTLIST','protocol_version':PROTOCOL_VERSION,'protocol_fingerprint':PROTOCOL_FINGERPRINT,'families_declared':list(DERIVATIVE_FAMILIES),'tested_results':res,'shortlist':passed,'data_failures':fail,'safety':safe,'note':'OKX-only historical derivative screen. Shortlisted mechanisms still require independent venue/forward confirmation.','real_orders_enabled':False,'live_permission':False};atomic(out/'STRATEGY_ATLAS_DERIVATIVES_RESULT.json',report);print('='*88);print('STRATEGY ATLAS DERIVATIVES FINAL');print('State:',report['state']);print('Shortlist:',[(x['symbol'],x['family']) for x in passed]);print('LIVE / real orders: DISABLED');print('='*88);return 0
if __name__=='__main__':raise SystemExit(main())
