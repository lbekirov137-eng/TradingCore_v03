#!/usr/bin/env python3
"""TradingCore Frontier Forward PAPER V1.1.
Replays only candles AFTER a frozen forward timestamp, with warmup data before it.
Public Binance spot data; long-only; 1x capital cap; TradingCore conservative costs.
No authenticated API and no real orders.

V1.1 execution fix: the trailing stop used for a candle is frozen before that
candle. A trail derived from the current close activates only on the next candle.
"""
from __future__ import annotations
import argparse,json,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from config.startup_safety import assert_safe_startup
from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs
from frontier_momentum_lab import SYMS,TFS,FAMS,TFMS,HOURS,features,signal,finite,stats,robust

BIN='https://data-api.binance.vision';CAPITAL=1000.0;RISK_USD=1.0

def req(url,attempts=4):
 last=None
 for n in range(attempts):
  try:
   with urlopen(Request(url,headers={'User-Agent':'TradingCore-FrontierForward/1.1','Accept':'application/json'}),timeout=20) as r:return json.loads(r.read().decode())
  except Exception as e:
   last=e
   if n+1<attempts:time.sleep(min(4,.4*(2**n)))
 raise RuntimeError(last)
def fetch(symbol,tf,freeze_ms):
 ms=TFMS[tf];now=int(time.time()*1000);end=(now//ms)*ms-1;start=freeze_ms-10*86_400_000;cur=start;out={}
 while cur<=end:
  data=req(f"{BIN}/api/v3/klines?{urlencode({'symbol':symbol,'interval':tf,'startTime':cur,'endTime':end,'limit':1000})}")
  if not isinstance(data,list) or not data:break
  last=None
  for r in data:
   try:
    ts=int(r[0]);last=ts
    if start<=ts<=end:out[ts]={'ts':ts,'o':float(r[1]),'h':float(r[2]),'l':float(r[3]),'c':float(r[4]),'v':float(r[5])}
   except Exception:pass
  if last is None or len(data)<1000:break
  nxt=last+ms
  if nxt<=cur:break
  cur=nxt;time.sleep(.01)
 return [out[k] for k in sorted(out)]
def sim(fam,symbol,tf,rows,freeze_ms):
 f=features(rows,tf);cfg=TradingCostConfig();pos=None;rs=[];signals=0;trades=[];maxbars=max(8,round(72/HOURS[tf]))
 for i in range(1,len(rows)):
  c=rows[i];x=f[i]
  if pos:
   active_trail=pos['trail'];exitp=reason=None
   if c['l']<=active_trail:exitp=active_trail;reason='TRAIL'
   elif finite(x['e20']) and c['c']<x['e20']:exitp=c['c'];reason='EMA_EXIT'
   elif i-pos['i']>=maxbars:exitp=c['c'];reason='TIME_EXIT'
   if exitp is not None:
    cc=compute_trade_costs(entry_price=pos['entry'],exit_price=exitp,quantity=pos['qty'],side='LONG',config=cfg);r=cc['net_pnl']/pos['risk'];rs.append(r);trades.append({'entry_utc':datetime.fromtimestamp(pos['ts']/1000,tz=timezone.utc).isoformat(),'exit_utc':datetime.fromtimestamp(c['ts']/1000,tz=timezone.utc).isoformat(),'entry':pos['entry'],'exit':exitp,'r':round(r,5),'net_pnl':cc['net_pnl'],'reason':reason});pos=None
   elif finite(x['atr']):
    pos['trail']=max(active_trail,c['c']-2.0*x['atr'])
  if pos or c['ts']<freeze_ms or not signal(fam,rows,f,i):continue
  a=x['atr'];signals+=1
  if not finite(a) or a<=0:continue
  entry=c['c'];stop=entry-1.5*a;unit=entry-stop
  if stop<=0 or unit<=0:continue
  qty=min(RISK_USD/unit,CAPITAL/entry);risk=qty*unit
  if qty>0 and risk>0:pos={'entry':entry,'trail':stop,'qty':qty,'risk':risk,'i':i,'ts':c['ts']}
 s=stats(rs);rb=robust(rs);soft=s['closed_trades']>=3 and finite(s['expectancy_r']) and s['expectancy_r']>0 and (not finite(s['profit_factor']) or s['profit_factor']>1)
 return {'lane_id':f'{symbol}:{tf}:{fam}','family':fam,'symbol':symbol,'timeframe':tf,'signals':signals,'closed_trades':s['closed_trades'],'stats':s,'segment_robustness':rb,'open_position':bool(pos),'recent_trades':trades[-10:],'promising_early':soft}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--state-dir',required=True);ap.add_argument('--freeze-utc',default='2026-08-18T18:45:00+00:00');a=ap.parse_args();safe=assert_safe_startup();freeze=datetime.fromisoformat(a.freeze_utc.replace('Z','+00:00'));freeze_ms=int(freeze.timestamp()*1000);market={};fail={}
 with ThreadPoolExecutor(max_workers=3) as pool:
  fut={pool.submit(fetch,s,t,freeze_ms):(s,t) for s in SYMS for t in TFS}
  for fu in as_completed(fut):
   k=fut[fu]
   try:market[k]=fu.result()
   except Exception as e:fail[':'.join(k)]=f'{type(e).__name__}: {e}'
 lanes=[]
 for s in SYMS:
  for tf in TFS:
   rows=market.get((s,tf)) or []
   if len(rows)<100:continue
   for fam in FAMS:lanes.append(sim(fam,s,tf,rows,freeze_ms))
 prom=[x for x in lanes if x['promising_early']];prom.sort(key=lambda x:(x['stats']['expectancy_r'] if finite(x['stats']['expectancy_r']) else -99,x['closed_trades']),reverse=True)
 out={'schema':'TRADINGCORE_FRONTIER_FORWARD_PAPER_V1_1','updated_at_utc':datetime.now(timezone.utc).isoformat(),'forward_freeze_utc':freeze.isoformat(),'lane_count':len(lanes),'total_closed_trade_observations_across_lanes':sum(x['closed_trades'] for x in lanes),'lanes_with_10_or_more':sum(x['closed_trades']>=10 for x in lanes),'promising_early':[x['lane_id'] for x in prom[:10]],'top_promising':prom[:10],'lanes':lanes,'data_failures':fail,'execution_model':{'entry':'signal candle close with conservative fee/slippage model','stop':'prior-candle frozen trail; current-close trail activates next candle','intrabar_lookahead_fixed':True},'safety':safe,'real_orders_enabled':False,'live_permission':False,'note':'Forward-only evidence. Early promising flags are not Champion or LIVE permission.'};root=Path(a.state_dir);root.mkdir(parents=True,exist_ok=True);(root/'FRONTIER_FORWARD_STATUS.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8');print('FRONTIER_FORWARD closed=',out['total_closed_trade_observations_across_lanes'],'promising=',out['promising_early'],'real_orders=False');return 0
if __name__=='__main__':raise SystemExit(main())
