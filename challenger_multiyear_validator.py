#!/usr/bin/env python3
"""TradingCore multi-year validator for the strongest historical challengers.

Frozen lanes only; no parameter search:
- XRPUSDT 4h RSI_PANIC_MEAN_REVERSION
- LTCUSDT 4h RANGE_FADE
- LTCUSDT 1h VOLATILITY_BREAKOUT_AFTER_DEAD_ZONE

Three years of public Binance + OKX spot candles, conservative TradingCore costs,
next-bar-open entries, conservative same-bar ambiguity (stop before target), and
four chronological segments. Research/PAPER only; no private API or orders.
"""
from __future__ import annotations
import argparse,json,math,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone,timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs
from api.strategy_engine.strategies.contracts import Candle
from config.startup_safety import assert_safe_startup
from strategy_atlas_price_v2 import features,sig,fin,meta

BIN='https://data-api.binance.vision';OKX='https://www.okx.com';DAY=86_400_000
YEARS=3
LANES=(
 ('XRPUSDT','4h','RSI_PANIC_MEAN_REVERSION'),
 ('LTCUSDT','4h','RANGE_FADE'),
 ('LTCUSDT','1h','VOLATILITY_BREAKOUT_AFTER_DEAD_ZONE'),
)
CAPITAL=1000.0;RISK_USD=1.0

def req(url,attempts=6):
 last=None
 for n in range(attempts):
  try:
   with urlopen(Request(url,headers={'User-Agent':'TradingCore-Challenger3Y/1.0','Accept':'application/json'}),timeout=25) as r:return json.loads(r.read().decode())
  except Exception as e:
   last=e
   if n+1<attempts:time.sleep(min(8,.5*(2**n)))
 raise RuntimeError(last)
def bounds(tf):
 ms,_,_=meta(tf);now=int(time.time()*1000);end=(now//ms)*ms-1;return end-int(YEARS*365.25*DAY),end
def fetch_binance(symbol,tf):
 ms,_,_=meta(tf);start,end=bounds(tf);cur=start;by={}
 while cur<=end:
  data=req(f"{BIN}/api/v3/klines?{urlencode({'symbol':symbol,'interval':tf,'startTime':cur,'endTime':end,'limit':1000})}")
  if not isinstance(data,list) or not data:break
  last=None
  for r in data:
   try:
    ts=int(r[0]);close=int(r[6]);last=ts
    if start<=ts and close<=end:by[ts]=Candle(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]))
   except Exception:pass
  if last is None or len(data)<1000:break
  nxt=last+ms
  if nxt<=cur:break
  cur=nxt;time.sleep(.015)
 return sorted(by.values(),key=lambda x:x.open_time_ms)
def fetch_okx(symbol,tf):
 _,bar,_=meta(tf);start,end=bounds(tf);inst=f'{symbol[:-4]}-USDT';cursor=None;by={}
 for _ in range(500):
  pars={'instId':inst,'bar':bar,'limit':100}
  if cursor is not None:pars['after']=str(cursor)
  d=req(f"{OKX}/api/v5/market/history-candles?{urlencode(pars)}")
  if not isinstance(d,dict) or str(d.get('code'))!='0':raise RuntimeError(d)
  rows=d.get('data') or []
  if not rows:break
  oldest=None
  for r in rows:
   try:
    ts=int(r[0]);oldest=ts if oldest is None else min(oldest,ts);confirmed=(str(r[8])=='1') if len(r)>8 else True
    if confirmed and start<=ts<=end:by[ts]=Candle(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]))
   except Exception:pass
  if oldest is None or oldest<=start or (cursor is not None and oldest>=cursor):break
  cursor=oldest;time.sleep(.12)
 return sorted(by.values(),key=lambda x:x.open_time_ms)
def stats(rs):
 n=len(rs);w=[x for x in rs if x>0];l=[x for x in rs if x<0];gp=sum(w);gl=-sum(l);eq=peak=dd=0.0
 for x in rs:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
 return {'closed_trades':n,'wins':len(w),'losses':len(l),'win_rate_percent':round(100*len(w)/n,2) if n else None,'net_r':round(sum(rs),4) if n else None,'profit_factor':round(gp/gl,4) if gl>1e-12 else (99.0 if w else None),'expectancy_r':round(sum(rs)/n,4) if n else None,'max_drawdown_r':round(dd,4) if n else None}
def simulate(symbol,tf,fam,rows):
 f,hours=features(rows,tf);cfg=TradingCostConfig();pos=None;pending=None;rs=[];trades=[];maxbars=max(2,24//hours)
 for i,c in enumerate(rows):
  # A signal is known only after its candle closes. Entry therefore occurs at
  # the NEXT candle open, not at the signal candle close.
  if pending is not None and pos is None:
   a=pending['atr'];entry=c.open;stop=entry-1.5*a;target=entry+3*a;unit=entry-stop
   if stop>0 and unit>0:
    qty=min(RISK_USD/unit,CAPITAL/entry);risk=qty*unit
    if qty>0 and risk>0:pos={'entry':entry,'stop':stop,'target':target,'qty':qty,'risk':risk,'i':i,'signal_ts':pending['ts']}
   pending=None
  if pos:
   exitp=reason=None
   # Conservative gaps and same-bar ambiguity: adverse stop is evaluated first.
   if c.open<=pos['stop']:exitp=c.open;reason='GAP_STOP'
   elif c.low<=pos['stop']:exitp=pos['stop'];reason='STOP'
   elif c.open>=pos['target']:exitp=c.open;reason='GAP_TARGET'
   elif c.high>=pos['target']:exitp=pos['target'];reason='TARGET'
   elif i-pos['i']>=maxbars:exitp=c.close;reason='TIME'
   if exitp is not None:
    cc=compute_trade_costs(entry_price=pos['entry'],exit_price=exitp,quantity=pos['qty'],side='LONG',config=cfg);r=cc['net_pnl']/pos['risk'];rs.append(r);trades.append({'signal_utc':datetime.fromtimestamp(pos['signal_ts']/1000,tz=timezone.utc).isoformat(),'exit_utc':datetime.fromtimestamp(c.open_time_ms/1000,tz=timezone.utc).isoformat(),'r':round(r,5),'reason':reason});pos=None
  if i+1<len(rows) and pos is None and pending is None and sig(fam,rows,f,i,hours):
   a=f[i]['atr']
   if fin(a) and a>0:pending={'atr':float(a),'ts':c.open_time_ms}
 full=stats(rs);segments=[]
 for k in range(4):
  a=(len(rs)*k)//4;b=(len(rs)*(k+1))//4;segments.append(stats(rs[a:b]))
 positive_segments=sum(1 for s in segments if isinstance(s.get('expectancy_r'),(int,float)) and s['expectancy_r']>0)
 checks={'trades':full['closed_trades']>=20,'pf':isinstance(full.get('profit_factor'),(int,float)) and full['profit_factor']>=1.2,'expectancy':isinstance(full.get('expectancy_r'),(int,float)) and full['expectancy_r']>=.05,'drawdown':isinstance(full.get('max_drawdown_r'),(int,float)) and full['max_drawdown_r']<=10,'segments':positive_segments>=3}
 return {'full':full,'segments':segments,'positive_segments':positive_segments,'checks':checks,'passed':all(checks.values()),'recent_trades':trades[-10:]}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output-dir',default='challenger_multiyear_runtime');a=ap.parse_args();safe=assert_safe_startup();market={};fail={}
 jobs=[]
 for v in ('BINANCE','OKX'):
  for s,tf,_ in LANES:
   key=(v,s,tf)
   if key not in jobs:jobs.append(key)
 with ThreadPoolExecutor(max_workers=4) as pool:
  fm={pool.submit(fetch_binance if v=='BINANCE' else fetch_okx,s,tf):(v,s,tf) for v,s,tf in jobs}
  for fu in as_completed(fm):
   k=fm[fu]
   try:market[k]=fu.result();print('CHALLENGER3Y_DATA',k,len(market[k]),flush=True)
   except Exception as e:fail[':'.join(k)]=f'{type(e).__name__}: {e}'
 outlanes=[]
 for s,tf,fam in LANES:
  venues={};allpass=True
  for v in ('BINANCE','OKX'):
   rows=market.get((v,s,tf)) or []
   if len(rows)<300:venues[v]={'error':'INSUFFICIENT_DATA','passed':False};allpass=False;continue
   r=simulate(s,tf,fam,rows);venues[v]=r;allpass=allpass and r['passed']
  outlanes.append({'lane_id':f'{s}:{tf}:{fam}','venues':venues,'passed_both_venues':allpass})
 passed=[x for x in outlanes if x['passed_both_venues']]
 out={'schema':'TRADINGCORE_CHALLENGER_MULTIYEAR_V1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'history_years':YEARS,'execution_model':'next-bar-open; conservative costs; stop-first intrabar ambiguity','lane_count':len(outlanes),'passing_both_venues':[x['lane_id'] for x in passed],'lanes':outlanes,'data_failures':fail,'safety':safe,'private_api_used':False,'real_orders_enabled':False,'live_permission':False,'note':'Multi-year robustness validator only. A pass still requires fresh forward execution evidence before any micro-live review.'};root=Path(a.output_dir);root.mkdir(parents=True,exist_ok=True);(root/'CHALLENGER_MULTIYEAR_RESULT.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8');print('CHALLENGER3Y passing=',out['passing_both_venues'],'fail=',fail,'LIVE=False');return 0
if __name__=='__main__':raise SystemExit(main())
