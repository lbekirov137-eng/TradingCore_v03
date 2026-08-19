#!/usr/bin/env python3
"""TradingCore Frontier Momentum Lab V1.1.
Fast historical discovery on BTC/ETH/SOL using 15m/30m/1h public Binance data.
Research/PAPER only. Long-only, 1x gross cap, conservative TradingCore costs.
No final-data tuning: a small frozen family map is evaluated chronologically.

V1.1 execution fix: the trailing stop used for a candle is frozen BEFORE that
candle. A new trail derived from the candle close only becomes active on the
next candle. This removes intrabar look-ahead from the original V1 simulator.
"""
from __future__ import annotations
import argparse,json,math,statistics,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from config.startup_safety import assert_safe_startup
from api.paper_trading.cost_model import TradingCostConfig,compute_trade_costs

BIN='https://data-api.binance.vision';DAY=86_400_000
SYMS=('BTCUSDT','ETHUSDT','SOLUSDT');TFS=('15m','30m','1h')
TFMS={'15m':900_000,'30m':1_800_000,'1h':3_600_000};HOURS={'15m':.25,'30m':.5,'1h':1.0}
FAMS=('TSMOM_VOL_24H','TSMOM_DUAL_24H_7D','DONCHIAN_48H','TSMOM_PULLBACK_RESUME')
CAPITAL=1000.0;RISK_USD=1.0

def get(url,attempts=5):
 last=None
 for n in range(attempts):
  try:
   with urlopen(Request(url,headers={'User-Agent':'TradingCore-Frontier/1.1','Accept':'application/json'}),timeout=25) as r:return json.loads(r.read().decode())
  except Exception as e:
   last=e
   if n+1<attempts:time.sleep(min(5,.5*(2**n)))
 raise RuntimeError(last)
def fetch(symbol,tf):
 ms=TFMS[tf];days=365 if tf=='1h' else 180;now=int(time.time()*1000);end=(now//ms)*ms-1;start=end-days*DAY;cur=start;out={}
 while cur<=end:
  data=get(f"{BIN}/api/v3/klines?{urlencode({'symbol':symbol,'interval':tf,'startTime':cur,'endTime':end,'limit':1000})}")
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
  cur=nxt;time.sleep(.015)
 return [out[k] for k in sorted(out)]
def ema(vals,n):
 z=[None]*len(vals)
 if len(vals)<n:return z
 cur=sum(vals[:n])/n;z[n-1]=cur;a=2/(n+1)
 for i in range(n,len(vals)):cur=a*vals[i]+(1-a)*cur;z[i]=cur
 return z
def atr(rows,n=14):
 z=[None]*len(rows);tr=[0.0]*len(rows)
 for i in range(1,len(rows)):
  c,p=rows[i],rows[i-1];tr[i]=max(c['h']-c['l'],abs(c['h']-p['c']),abs(c['l']-p['c']))
 if len(rows)<=n:return z
 cur=sum(tr[1:n+1])/n;z[n]=cur
 for i in range(n+1,len(rows)):cur=((n-1)*cur+tr[i])/n;z[i]=cur
 return z
def features(rows,tf):
 c=[x['c'] for x in rows];e20=ema(c,20);e50=ema(c,50);aa=atr(rows);bh24=max(4,round(24/HOURS[tf]));bh7=max(bh24+1,round(168/HOURS[tf]));bh48=max(8,round(48/HOURS[tf]));ret=[0.0]+[c[i]/c[i-1]-1 for i in range(1,len(c))];out=[]
 for i,x in enumerate(rows):
  r24=c[i]/c[i-bh24]-1 if i>=bh24 else None;r7=c[i]/c[i-bh7]-1 if i>=bh7 else None
  if i>=bh24:
   w=ret[i-bh24+1:i+1];sd=statistics.pstdev(w) if len(w)>1 else 0;z24=r24/(sd*math.sqrt(len(w))) if sd>1e-12 else None
  else:z24=None
  h48=max(y['h'] for y in rows[i-bh48:i]) if i>=bh48 else None
  out.append({'e20':e20[i],'e50':e50[i],'atr':aa[i],'ret24':r24,'ret7':r7,'z24':z24,'h48':h48})
 return out
def finite(x):return isinstance(x,(int,float)) and math.isfinite(float(x))
def signal(fam,rows,f,i):
 x=f[i];c=rows[i];p=rows[i-1] if i else c
 if i<60 or not all(finite(x.get(k)) for k in ('e20','e50','atr')):return False
 trend=c['c']>x['e20']>x['e50'];bull=c['c']>c['o']
 if fam=='TSMOM_VOL_24H':return trend and finite(x['z24']) and x['z24']>=.8
 if fam=='TSMOM_DUAL_24H_7D':return trend and finite(x['ret24']) and finite(x['ret7']) and x['ret24']>=.01 and x['ret7']>=.03
 if fam=='DONCHIAN_48H':return trend and finite(x['h48']) and c['c']>x['h48'] and bull
 if fam=='TSMOM_PULLBACK_RESUME':return finite(x['ret7']) and x['ret7']>=.03 and c['c']>x['e50'] and c['l']<=x['e20']<c['c'] and bull and c['c']>p['c']
 return False
def stats(rs):
 n=len(rs);wins=[r for r in rs if r>0];loss=[r for r in rs if r<0];gp=sum(wins);gl=-sum(loss);eq=peak=dd=0.0
 for r in rs:eq+=r;peak=max(peak,eq);dd=max(dd,peak-eq)
 return {'closed_trades':n,'wins':len(wins),'losses':len(loss),'win_rate_percent':round(100*len(wins)/n,2) if n else None,'net_r':round(sum(rs),4) if n else None,'profit_factor':round(gp/gl,4) if gl>1e-12 else (None if not wins else 99.0),'expectancy_r':round(sum(rs)/n,4) if n else None,'max_drawdown_r':round(dd,4) if n else None}
def robust(rs):
 if len(rs)<8:return None
 chunks=[];q=max(1,len(rs)//4)
 for k in range(4):
  z=rs[k*q:(k+1)*q if k<3 else len(rs)]
  if z:chunks.append(sum(z)>0)
 return round(sum(chunks)/len(chunks),3) if chunks else None
def simulate(fam,symbol,tf,rows):
 f=features(rows,tf);cfg=TradingCostConfig();pos=None;rs=[];signals=0;maxbars=max(8,round(72/HOURS[tf]))
 for i in range(1,len(rows)):
  c=rows[i];x=f[i]
  if pos:
   # IMPORTANT: use the trail that existed BEFORE this candle. The trail
   # derived from this candle close becomes active only for the next candle.
   active_trail=pos['trail'];exitp=None
   if c['l']<=active_trail:exitp=active_trail
   elif finite(x['e20']) and c['c']<x['e20']:exitp=c['c']
   elif i-pos['i']>=maxbars:exitp=c['c']
   if exitp is not None:
    cc=compute_trade_costs(entry_price=pos['entry'],exit_price=exitp,quantity=pos['qty'],side='LONG',config=cfg);rs.append(cc['net_pnl']/pos['risk']);pos=None
   elif finite(x['atr']):
    pos['trail']=max(active_trail,c['c']-2.0*x['atr'])
  if pos or not signal(fam,rows,f,i):continue
  a=x['atr'];signals+=1
  if not finite(a) or a<=0:continue
  entry=c['c'];stop=entry-1.5*a;unit=entry-stop
  if stop<=0 or unit<=0:continue
  qty=min(RISK_USD/unit,CAPITAL/entry);risk=qty*unit
  if qty>0 and risk>0:pos={'entry':entry,'trail':stop,'qty':qty,'risk':risk,'i':i}
 mid=max(1,len(rs)//2);a=rs[:mid];b=rs[mid:];full=stats(rs);sa=stats(a);sb=stats(b);rb=robust(rs);minn=40 if tf!='1h' else 30
 checks={'trades':full['closed_trades']>=minn,'pf':finite(full['profit_factor']) and full['profit_factor']>=1.15,'exp':finite(full['expectancy_r']) and full['expectancy_r']>=.05,'dd':finite(full['max_drawdown_r']) and full['max_drawdown_r']<=10,'first_positive':finite(sa['expectancy_r']) and sa['expectancy_r']>0,'second_positive':finite(sb['expectancy_r']) and sb['expectancy_r']>0,'robust':finite(rb) and rb>=.5}
 return {'family':fam,'symbol':symbol,'timeframe':tf,'signals':signals,'full':full,'first_half':sa,'second_half':sb,'segment_robustness':rb,'checks':checks,'passed':all(checks.values())}
def rank(x):
 s=x['full'];n=s['closed_trades'];exp=s['expectancy_r'] if finite(s['expectancy_r']) else -99;pf=s['profit_factor'] if finite(s['profit_factor']) else 0;dd=s['max_drawdown_r'] if finite(s['max_drawdown_r']) else 99
 return exp*math.sqrt(max(n,1))*min(pf,3)/(1+dd/10)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output-dir',required=True);a=ap.parse_args();safe=assert_safe_startup();market={};fail={}
 with ThreadPoolExecutor(max_workers=3) as pool:
  fut={pool.submit(fetch,s,t):(s,t) for s in SYMS for t in TFS}
  for fu in as_completed(fut):
   k=fut[fu]
   try:market[k]=fu.result();print('FRONTIER_DATA',k,'bars',len(market[k]),flush=True)
   except Exception as e:fail[':'.join(k)]=f'{type(e).__name__}: {e}'
 lanes=[]
 for s in SYMS:
  for tf in TFS:
   rows=market.get((s,tf)) or []
   if len(rows)<300:continue
   for fam in FAMS:lanes.append(simulate(fam,s,tf,rows))
 lanes.sort(key=rank,reverse=True);passed=[x for x in lanes if x['passed']];chall=[x for x in lanes if x['full']['closed_trades']>=8 and finite(x['full']['expectancy_r']) and x['full']['expectancy_r']>0][:12]
 out={'schema':'TRADINGCORE_FRONTIER_MOMENTUM_V1_1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'state':'FRONTIER_CANDIDATE_FOUND_NOT_LIVE' if passed else 'NO_FRONTIER_CHAMPION_YET','lane_count':len(lanes),'passing_lane_count':len(passed),'candidate':passed[0] if passed else None,'passing_lanes':[{k:x[k] for k in ('family','symbol','timeframe')} for x in passed],'top_challengers':chall,'top_ranked':lanes[:15],'data_failures':fail,'execution_model':{'entry':'signal candle close with conservative fee/slippage model','stop':'prior-candle frozen trail; current-close trail activates next candle','intrabar_lookahead_fixed':True},'safety':safe,'real_orders_enabled':False,'live_permission':False,'note':'Discovery screen only. Any passing lane still requires independent venue and fresh forward confirmation.'};d=Path(a.output_dir);d.mkdir(parents=True,exist_ok=True);(d/'FRONTIER_MOMENTUM_RESULT.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8');print('FRONTIER_MOMENTUM',out['state'],'passing',len(passed),'top',[(x['family'],x['symbol'],x['timeframe'],x['full']) for x in lanes[:3]]);return 0
if __name__=='__main__':raise SystemExit(main())
