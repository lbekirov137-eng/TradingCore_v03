#!/usr/bin/env python3
"""Evaluate accumulated TradingCore microstructure snapshots chronologically.
Research/PAPER only. Uses future snapshots as labels; never enables LIVE.
"""
from __future__ import annotations
import argparse,json,math,statistics
from datetime import datetime,timezone
from pathlib import Path
from config.startup_safety import assert_safe_startup

def finite(x):return isinstance(x,(int,float)) and math.isfinite(float(x))
def summarize(vals):
 if not vals:return {'n':0,'avg_raw_bps':None,'hit_rate_percent':None,'t_like':None}
 avg=statistics.fmean(vals);sd=statistics.pstdev(vals) if len(vals)>1 else 0;t=avg/(sd/math.sqrt(len(vals))) if sd>1e-12 else None
 return {'n':len(vals),'avg_raw_bps':round(avg*10000,4),'hit_rate_percent':round(100*sum(v>0 for v in vals)/len(vals),2),'t_like':round(t,4) if finite(t) else None}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--state-dir',required=True);ap.add_argument('--output-dir',required=True);a=ap.parse_args();safe=assert_safe_startup();p=Path(a.state_dir)/'strategy_atlas_microstructure_runtime/MICROSTRUCTURE_SNAPSHOTS.jsonl';series={s:[] for s in ('BTC','ETH','SOL')};errors=[]
 if p.exists():
  for line in p.read_text(encoding='utf-8').splitlines():
   try:d=json.loads(line)
   except Exception:continue
   ts=d.get('recorded_at_utc')
   for x in d.get('symbols') or []:
    s=x.get('symbol');spot=x.get('okx_spot') or {};bid=spot.get('bid');ask=spot.get('ask')
    if s not in series or not finite(bid) or not finite(ask):continue
    mid=(float(bid)+float(ask))/2;flow=(x.get('okx_trade_flow') or {}).get('signed_imbalance')
    series[s].append({'ts':ts,'mid':mid,'depth':x.get('okx_orderbook_imbalance'),'top':x.get('okx_top_level_imbalance'),'flow':flow,'mp':x.get('okx_microprice_edge'),'basis':x.get('spot_perp_basis'),'funding':x.get('funding_rate'),'cross':(x.get('cross_exchange') or {}).get('best')})
 tests=[]
 def run(name,field,pred):
  for s,rows in series.items():
   for h in (1,3,6):
    vals=[]
    for i,x in enumerate(rows[:-h] if h else rows):
     v=x.get(field)
     if finite(v) and pred(float(v)):
      y=rows[i+h]['mid']/x['mid']-1;vals.append(y)
    z=summarize(vals);z.update({'signal':name,'symbol':s,'horizon_snapshots':h});tests.append(z)
 run('DEPTH_HIGH_CONTINUATION','depth',lambda v:v>=.68);run('DEPTH_LOW_REVERSION_LONG','depth',lambda v:v<=.32);run('TOP_HIGH_CONTINUATION','top',lambda v:v>=.68);run('FLOW_BUY_CONTINUATION','flow',lambda v:v>=.35);run('FLOW_SELL_REVERSION_LONG','flow',lambda v:v<=-.35);run('MICROPRICE_POSITIVE','mp',lambda v:v>=.00005);run('NEGATIVE_BASIS_REVERSION','basis',lambda v:v<=-.003);run('NEGATIVE_FUNDING_REVERSION','funding',lambda v:v<=-.0005)
 ranked=sorted([x for x in tests if x['n']>=5 and finite(x['avg_raw_bps'])],key=lambda x:(x['t_like'] if finite(x['t_like']) else -99,x['avg_raw_bps']),reverse=True)
 cross=[]
 for s,rows in series.items():
  vals=[x['cross'] for x in rows if finite(x.get('cross'))];cross.append({'symbol':s,'observations':len(vals),'positive_net_quotes':sum(v>0 for v in vals),'best_net_quote':max(vals) if vals else None})
 out={'schema':'TRADINGCORE_FRONTIER_MICROSTRUCTURE_EVAL_V1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'snapshots_by_symbol':{s:len(v) for s,v in series.items()},'tests':tests,'top_predictive_signals':ranked[:15],'cross_exchange_feasibility':cross,'safety':safe,'real_orders_enabled':False,'live_permission':False,'note':'Raw predictive returns are not executable PnL. Costs, latency and queue position must be modeled before promotion.'};d=Path(a.output_dir);d.mkdir(parents=True,exist_ok=True);(d/'FRONTIER_MICROSTRUCTURE_RESULT.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8');print('FRONTIER_MICRO_EVAL',out['snapshots_by_symbol'],'top',ranked[:3]);return 0
if __name__=='__main__':raise SystemExit(main())
