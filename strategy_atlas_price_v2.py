#!/usr/bin/env python3
"""Strategy Atlas V1 price/niche replay: Binance + OKX, 1H/4H, no tuning.
Research/PAPER only. No authenticated API and no order path.
"""
from __future__ import annotations
import argparse, json, math, statistics, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_engine.strategies.contracts import Candle
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from config.startup_safety import assert_safe_startup
from strategy_atlas_protocol import *

BINANCE="https://data-api.binance.vision"; OKX="https://www.okx.com"; DAY=86_400_000
FAMILIES=PRICE_FAMILIES+tuple(x for x in NICHE_FAMILIES if not x.startswith("CORRELATION_BREAK_"))

def atomic(p:Path,d:dict):
 p.parent.mkdir(parents=True,exist_ok=True); q=p.with_suffix(p.suffix+".tmp"); q.write_text(json.dumps(d,indent=2,default=str),encoding="utf-8"); q.replace(p)

def req(url:str,attempts:int=6):
 last=None
 for n in range(attempts):
  try:
   with urlopen(Request(url,headers={"User-Agent":"TradingCore-Atlas/1.0","Accept":"application/json"}),timeout=25) as r:return json.loads(r.read().decode())
  except (HTTPError,URLError,TimeoutError,json.JSONDecodeError) as e:
   last=e
   if n+1<attempts:time.sleep(min(8,.5*(2**n)))
 raise RuntimeError(last)

def meta(tf:str):
 return (3_600_000,"1H",1) if tf=="1h" else (14_400_000,"4H",4)

def window(tf:str):
 ms,_,_=meta(tf); now=int(time.time()*1000); end=(now//ms)*ms-1; return end-SPOT_HISTORY_DAYS*DAY,end

def binance(symbol:str,tf:str):
 ms,_,_=meta(tf); start,end=window(tf); cur=start; by={}
 while cur<=end:
  data=req(f"{BINANCE}/api/v3/klines?{urlencode({'symbol':symbol,'interval':tf,'startTime':cur,'endTime':end,'limit':1000})}")
  if not isinstance(data,list) or not data:break
  last=None
  for r in data:
   try:
    ts=int(r[0]); close=int(r[6])
    if start<=ts and close<=end:by[ts]=Candle(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])); last=ts
   except:pass
  if last is None or len(data)<1000:break
  nxt=last+ms
  if nxt<=cur:break
  cur=nxt; time.sleep(.02)
 return sorted(by.values(),key=lambda x:x.open_time_ms)

def okx(symbol:str,tf:str):
 _,bar,_=meta(tf); start,end=window(tf); inst=f"{symbol[:-4]}-USDT"; cursor=None; by={}
 for _ in range(180):
  pars={'instId':inst,'bar':bar,'limit':100}
  if cursor is not None:pars['after']=str(cursor)
  data=req(f"{OKX}/api/v5/market/history-candles?{urlencode(pars)}")
  if not isinstance(data,dict) or str(data.get('code'))!='0':raise RuntimeError(data)
  rows=data.get('data') or []
  if not rows:break
  oldest=None
  for r in rows:
   try:
    ts=int(r[0]); oldest=ts if oldest is None else min(oldest,ts)
    confirmed=(str(r[8])=='1') if len(r)>8 else True
    if confirmed and start<=ts<=end:by[ts]=Candle(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]))
   except:pass
  if oldest is None or oldest<=start or (cursor is not None and oldest>=cursor):break
  cursor=oldest; time.sleep(.22)
 return sorted(by.values(),key=lambda x:x.open_time_ms)

def ema(v,n):
 out=[None]*len(v)
 if len(v)<n:return out
 cur=sum(v[:n])/n;out[n-1]=cur;a=2/(n+1)
 for i in range(n,len(v)):cur=a*v[i]+(1-a)*cur;out[i]=cur
 return out

def atr(rows,n=14):
 out=[None]*len(rows);tr=[0.0]*len(rows)
 for i in range(1,len(rows)):
  c,p=rows[i],rows[i-1];tr[i]=max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close))
 if len(rows)<=n:return out
 s=sum(tr[1:n+1]);out[n]=s/n
 for i in range(n+1,len(rows)):s+=tr[i]-tr[i-n];out[i]=s/n
 return out

def rsi(v,n=14):
 out=[None]*len(v);g=[0.0]*len(v);l=[0.0]*len(v)
 for i in range(1,len(v)):
  d=v[i]-v[i-1];g[i]=max(d,0);l[i]=max(-d,0)
 if len(v)<=n:return out
 sg=sum(g[1:n+1]);sl=sum(l[1:n+1])
 for i in range(n,len(v)):
  if i>n:sg+=g[i]-g[i-n];sl+=l[i]-l[i-n]
  ag=sg/n;al=sl/n;out[i]=100 if al<=1e-12 else 100-100/(1+ag/al)
 return out

def features(rows,tf):
 c=[x.close for x in rows];vol=[x.volume for x in rows];e20=ema(c,20);e50=ema(c,50);e200=ema(c,200);aa=atr(rows);rr=rsi(c);hours=meta(tf)[2];b24=max(1,24//hours);b7=max(2,168//hours);out=[]
 for i,x in enumerate(rows):
  d={'e20':e20[i],'e50':e50[i],'e200':e200[i],'atr':aa[i],'rsi':rr[i],'ret24':x.close/c[i-b24]-1 if i>=b24 else None,'ret7':x.close/c[i-b7]-1 if i>=b7 else None}
  if i>=20:
   w=rows[i-20:i];vals=c[i-20:i];m=statistics.fmean(vals);sd=statistics.pstdev(vals);d.update(h20=max(z.high for z in w),l20=min(z.low for z in w),sma=m,bbl=m-2*sd,bbh=m+2*sd,v20=statistics.fmean(vol[i-20:i]))
  else:d.update(h20=None,l20=None,sma=None,bbl=None,bbh=None,v20=None)
  if i>=48:d.update(h48=max(z.high for z in rows[i-48:i]),l48=min(z.low for z in rows[i-48:i]))
  else:d.update(h48=None,l48=None)
  if i>=100 and aa[i] is not None:
   h=[z for z in aa[i-100:i] if isinstance(z,(int,float)) and z>0];d['arank']=sum(z<=aa[i] for z in h)/len(h) if h else None
  else:d['arank']=None
  out.append(d)
 return out,hours

def fin(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(float(x))

def sig(fam,rows,f,i,hours):
 if i<201:return False
 c,p=rows[i],rows[i-1];x,y=f[i],f[i-1]
 if not all(fin(x[k]) for k in ('e20','e50','e200','atr','rsi')):return False
 e20,e50,e200=map(float,(x['e20'],x['e50'],x['e200']));trend=e50>e200;gap=abs(e20-e50)/c.close;bull=c.close>c.open
 if fam=='TREND_DONCHIAN_BREAKOUT':return trend and fin(x['h48']) and c.close>float(x['h48']) and bull
 if fam=='TREND_EMA_PULLBACK':return trend and c.low<=e20<c.close and bull
 if fam=='TIME_SERIES_MOMENTUM':return trend and fin(x['ret7']) and x['ret7']>=.04 and c.close>p.close
 if fam=='VOLUME_CONFIRMED_BREAKOUT':return trend and fin(x['h20']) and fin(x['v20']) and c.close>x['h20'] and c.volume>=1.5*x['v20']
 if fam=='VOL_COMPRESSION_BREAKOUT':return trend and fin(x['arank']) and x['arank']<=.25 and fin(x['h20']) and c.close>x['h20']
 if fam=='RSI_PANIC_MEAN_REVERSION':return x['rsi']<=25 and fin(x['ret24']) and x['ret24']<=-.04 and bull
 if fam=='BOLLINGER_REENTRY_MEAN_REVERSION':return fin(y['bbl']) and fin(x['bbl']) and p.close<y['bbl'] and c.close>x['bbl'] and bull
 if fam=='RANGE_FADE':return gap<=.004 and fin(x['bbl']) and c.low<x['bbl']<c.close and bull
 if fam=='OPENING_RANGE_BREAKOUT_UTC':
  dt=datetime.fromtimestamp(c.open_time_ms/1000,tz=timezone.utc); ds=c.open_time_ms-(dt.hour*60+dt.minute)*60_000; day=[z for z in rows[max(0,i-max(24,24//hours)):i] if z.open_time_ms>=ds]; n=max(1,4//hours)
  return len(day)>=n and dt.hour>=4 and trend and c.close>max(z.high for z in day[:n]) and bull
 if fam=='SESSION_MOMENTUM_ASIA_EU_US':
  hr=datetime.fromtimestamp(c.open_time_ms/1000,tz=timezone.utc).hour;return trend and hr in {0,8,12,16} and fin(x['ret24']) and x['ret24']>=.015 and bull
 if fam=='WEEKEND_WEEKDAY_ROTATION':
  dt=datetime.fromtimestamp(c.open_time_ms/1000,tz=timezone.utc);return trend and dt.weekday() in {0,4} and dt.hour==0 and fin(x['ret7']) and x['ret7']>0
 if fam=='VOLATILITY_REGIME_SWITCH':
  rank=x['arank'] if fin(x['arank']) else .5;return (trend and rank>=.60 and fin(x['h20']) and c.close>x['h20']) or (gap<=.004 and rank<=.35 and fin(x['bbl']) and c.low<x['bbl'] and bull)
 if fam=='GRID_RANGE_CAPTURE':return gap<=.003 and x['rsi']<=40 and fin(x['sma']) and c.close<x['sma'] and bull
 if fam=='VOLATILITY_BREAKOUT_AFTER_DEAD_ZONE':return trend and fin(x['arank']) and x['arank']<=.20 and fin(x['h48']) and c.close>x['h48']
 if fam=='FAILED_BREAKOUT_FADE':return fin(y['l20']) and p.low<y['l20']<p.close and bull and c.close>p.close
 if fam=='LIQUIDITY_SWEEP_RECLAIM_PROXY':return fin(x['l20']) and c.low<x['l20']<c.close and bull
 if fam=='DAY_OF_WEEK_EFFECT':
  dt=datetime.fromtimestamp(c.open_time_ms/1000,tz=timezone.utc);return trend and dt.weekday()==0 and dt.hour==0
 if fam=='UTC_TIME_BUCKET_EFFECT':
  hr=datetime.fromtimestamp(c.open_time_ms/1000,tz=timezone.utc).hour;return trend and hr in {0,8,16} and bull and c.close>e20
 return False

def robust(trades,segments=4):
 if len(trades)<8:return None
 n=max(1,len(trades)//segments);good=used=0
 for k in range(segments):
  b=trades[k*n:(k+1)*n if k<segments-1 else len(trades)]
  if not b:continue
  used+=1;s=build_stats(b);good+=int(fin(s.get('net_pnl')) and s['net_pnl']>0)
 return round(good/used,4) if used else None

def simulate(fam,symbol,tf,rows):
 f,hours=features(rows,tf);cost=TradingCostConfig();trades=[];pos=None;signals=0;maxbars=max(2,24//hours);ims=meta(tf)[0]
 for i,c in enumerate(rows):
  if pos:
   xp=pos['stop'] if c.low<=pos['stop'] else pos['target'] if c.high>=pos['target'] else c.close if i-pos['i']>=maxbars else None
   if xp is not None:
    cc=compute_trade_costs(entry_price=pos['entry'],exit_price=xp,quantity=pos['qty'],side='LONG',config=cost);r=cc['net_pnl']/pos['risk'];trades.append(ClosedTrade(f'{fam}:{symbol}:{tf}',datetime.fromtimestamp((c.open_time_ms+ims)/1000,tz=timezone.utc).isoformat(),symbol,float(cc['net_pnl']),r));pos=None
  if pos or not sig(fam,rows,f,i,hours):continue
  a=f[i]['atr']
  if not fin(a) or a<=0:continue
  signals+=1;entry=c.close;stop=entry-1.5*a;target=entry+3*a
  if stop<=0:continue
  unit=entry-stop;qty=min(RISK_AMOUNT_USD/unit,REFERENCE_CAPITAL_USD/entry);risk=qty*unit
  if qty>0 and risk>0:pos={'entry':entry,'stop':stop,'target':target,'qty':qty,'risk':risk,'i':i}
 return {'signals':signals,'trades':trades,'stats':build_stats(trades),'robustness':robust(trades)}

def num(s,k,fb=-1e99):return float(s[k]) if fin(s.get(k)) else fb

def evaluate(lane,mid):
 t=lane['trades'];a=[x for x in t if str(x.closed_at_utc)<mid];b=[x for x in t if str(x.closed_at_utc)>=mid];full=build_stats(t);sa=build_stats(a);sb=build_stats(b);rb=robust(t)
 ck={'n':int(full.get('closed_trades') or 0)>=MIN_TRADES_PER_VENUE,'pf':num(full,'profit_factor')>=MIN_FULL_PF,'exp':num(full,'expectancy_r')>=MIN_FULL_EXPECTANCY_R,'net':num(full,'net_pnl')>0,'dd':0<=num(full,'max_drawdown_r',1e99)<=MAX_FULL_DD_R,'robust':fin(rb) and rb>=MIN_SEGMENT_ROBUSTNESS,'a_n':int(sa.get('closed_trades') or 0)>=MIN_TRADES_PER_HALF,'a_pf':num(sa,'profit_factor')>=MIN_HALF_PF,'a_exp':num(sa,'expectancy_r')>MIN_HALF_EXPECTANCY_R,'b_n':int(sb.get('closed_trades') or 0)>=MIN_TRADES_PER_HALF,'b_pf':num(sb,'profit_factor')>=MIN_HALF_PF,'b_exp':num(sb,'expectancy_r')>MIN_HALF_EXPECTANCY_R}
 return {'full':full,'first_half':sa,'second_half':sb,'segment_robustness':rb,'checks':ck,'passed':all(ck.values())}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output',default='strategy_atlas_price_runtime');args=ap.parse_args();safe=assert_safe_startup();out=Path(args.output);out.mkdir(parents=True,exist_ok=True);market={};fail={}
 jobs=[(v,s,tf) for v in ('BINANCE','OKX') for s in SPOT_SYMBOLS for tf in SPOT_TIMEFRAMES]
 with ThreadPoolExecutor(max_workers=4) as pool:
  fm={pool.submit(binance if v=='BINANCE' else okx,s,tf):(v,s,tf) for v,s,tf in jobs}
  for fu in as_completed(fm):
   key=fm[fu];lab=':'.join(key)
   try:market[key]=fu.result();print('ATLAS_DATA',lab,'bars=',len(market[key]),flush=True)
   except Exception as e:fail[lab]=f'{type(e).__name__}: {e}';print('ATLAS_DATA_FAIL',lab,fail[lab],flush=True)
 res=[]
 for fam in FAMILIES:
  for s in SPOT_SYMBOLS:
   for tf in SPOT_TIMEFRAMES:
    st,en=window(tf);mid=datetime.fromtimestamp((st+(en-st)//2)/1000,tz=timezone.utc).isoformat();venues={};ok=True
    for v in ('BINANCE','OKX'):
     rows=market.get((v,s,tf)) or []
     if len(rows)<500:venues[v]={'passed':False,'error':'INSUFFICIENT_DATA'};ok=False;continue
     ev=evaluate(simulate(fam,s,tf,rows),mid);venues[v]=ev;ok=ok and ev['passed']
    state='ATLAS_PRICE_PASS_NOT_LIVE' if ok else 'ATLAS_PRICE_REJECT';res.append({'family':fam,'symbol':s,'timeframe':tf,'state':state,'venues':venues});print('ATLAS_PRICE',fam,s,tf,state,flush=True)
 passed=[x for x in res if x['state']=='ATLAS_PRICE_PASS_NOT_LIVE']
 def rk(x):
  p=[];e=[];n=0
  for v in ('BINANCE','OKX'):
   f=((x['venues'].get(v) or {}).get('full') or {});p.append(num(f,'profit_factor'));e.append(num(f,'expectancy_r'));n+=int(f.get('closed_trades') or 0)
  return min(p),min(e),n
 passed.sort(key=rk,reverse=True);report={'schema':'TRADINGCORE_STRATEGY_ATLAS_PRICE_V1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'state':'ATLAS_PRICE_CANDIDATE_FOUND_NOT_LIVE' if passed else 'NO_ATLAS_PRICE_CANDIDATE','protocol_version':PROTOCOL_VERSION,'protocol_fingerprint':PROTOCOL_FINGERPRINT,'families':list(FAMILIES),'lane_count':len(res),'passing_lane_count':len(passed),'candidate':passed[0] if passed else None,'passing_lanes':[{k:x[k] for k in ('family','symbol','timeframe')} for x in passed],'data_failures':fail,'lanes':res,'safety':safe,'real_orders_enabled':False,'live_permission':False}
 atomic(out/'STRATEGY_ATLAS_PRICE_RESULT.json',report);print('='*92);print('STRATEGY ATLAS PRICE FINAL RESULT');print('State:',report['state']);print('Passing:',len(passed),'/',len(res));print('Candidate:',report['candidate'] and (report['candidate']['family'],report['candidate']['symbol'],report['candidate']['timeframe']));print('LIVE / real orders: DISABLED');print('='*92);return 0
if __name__=='__main__':raise SystemExit(main())
