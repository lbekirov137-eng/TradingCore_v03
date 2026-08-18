#!/usr/bin/env python3
"""Mine prior TradingCore research for promising-but-not-yet-valid challengers.
PAPER/research only; never grants LIVE permission.
"""
from __future__ import annotations
import argparse,json,math
from datetime import datetime,timezone
from pathlib import Path
from config.startup_safety import assert_safe_startup

def load(p):
 try:return json.loads(Path(p).read_text(encoding='utf-8'))
 except Exception:return None
def finite(x):return isinstance(x,(int,float)) and math.isfinite(float(x))
def add(out,source,lane_id,venue,stats,state=None):
 n=int(stats.get('closed_trades') or stats.get('trades') or 0);pf=stats.get('profit_factor');exp=stats.get('expectancy_r')
 if exp is None and finite(stats.get('avg_return_bps')):exp=float(stats['avg_return_bps'])/100.0
 net=stats.get('net_pnl');dd=stats.get('max_drawdown_r')
 if n<3:return
 if not finite(exp):return
 score=float(exp)*math.sqrt(n)
 if finite(pf):score*=min(float(pf),3.0)/1.2
 if finite(dd):score/=1+max(float(dd),0)/10
 out.append({'source':source,'lane_id':lane_id,'venue':venue,'closed_trades':n,'profit_factor':pf,'expectancy_r':exp,'net_pnl':net,'max_drawdown_r':dd,'source_state':state,'challenger_score':round(score,6)})
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--state-dir',required=True);ap.add_argument('--output-dir',required=True);a=ap.parse_args();assert_safe_startup();root=Path(a.state_dir);rows=[]
 r=load(root/'rapid_replay_runtime/RAPID_REPLAY_RESULT.json') or {}
 for lane in r.get('lanes') or []:
  for venue,p in (lane.get('venues') or {}).items():
   s=((p.get('stats') or {}).get('full') or {}) if isinstance(p,dict) else {}
   add(rows,'RAPID_REPLAY',lane.get('lane_id'),venue,s,lane.get('state'))
 p=load(root/'strategy_atlas_price_runtime/STRATEGY_ATLAS_PRICE_RESULT.json') or {}
 for lane in p.get('lanes') or []:
  lid=':'.join(str(lane.get(k)) for k in ('symbol','timeframe','family'))
  for venue,v in (lane.get('venues') or {}).items():
   s=(v.get('full') or {}) if isinstance(v,dict) else {}
   add(rows,'PRICE_ATLAS',lid,venue,s,lane.get('state'))
 rows.sort(key=lambda x:(x['challenger_score'],x['closed_trades']),reverse=True)
 positive=[x for x in rows if float(x.get('expectancy_r') or -999)>0 and (not finite(x.get('profit_factor')) or float(x['profit_factor'])>1)]
 out={'schema':'TRADINGCORE_FRONTIER_HISTORY_V1','generated_at_utc':datetime.now(timezone.utc).isoformat(),'past_observations_ranked':len(rows),'positive_challengers':positive[:30],'top_all':rows[:30],'note':'Historical evidence only. Small samples and rejected lanes remain challengers, never LIVE permission.','real_orders_enabled':False,'live_permission':False}
 d=Path(a.output_dir);d.mkdir(parents=True,exist_ok=True);(d/'FRONTIER_HISTORY_RANKING.json').write_text(json.dumps(out,indent=2,default=str),encoding='utf-8');print('FRONTIER_HISTORY positive=',len(positive),'top=',positive[:3]);return 0
if __name__=='__main__':raise SystemExit(main())
