#!/usr/bin/env python3
"""Forward microstructure scout for TradingCore.
Research/PAPER only. Public data only. No accounts, balances, transfers or orders.
Collects L2 state, recent aggressive trade flow, microprice, spread, basis/funding and cross-venue feasibility.
"""
from __future__ import annotations
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from config.startup_safety import assert_safe_startup
from strategy_atlas_protocol import MICROSTRUCTURE_FAMILIES,PROTOCOL_FINGERPRINT,PROTOCOL_VERSION

BIN='https://data-api.binance.vision';OKX='https://www.okx.com';TAKER=.001;SLIP=.0005
SYMS=('BTC','ETH','SOL')

def get(url):
 with urlopen(Request(url,headers={'User-Agent':'TradingCore-MicroAtlas/2.0','Accept':'application/json'}),timeout=20) as r:return json.loads(r.read().decode())
def atomic(p,d):
 p.parent.mkdir(parents=True,exist_ok=True);q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(d,indent=2,default=str),encoding='utf-8');q.replace(p)
def bbook(symbol):
 d=get(f"{BIN}/api/v3/ticker/bookTicker?symbol={symbol}");return {'bid':float(d['bidPrice']),'ask':float(d['askPrice']),'bid_sz':float(d['bidQty']),'ask_sz':float(d['askQty'])}
def obook(inst):
 d=get(f"{OKX}/api/v5/market/books?{urlencode({'instId':inst,'sz':20})}");x=d['data'][0];b=x['bids'];a=x['asks'];return {'bid':float(b[0][0]),'ask':float(a[0][0]),'bid_sz':float(b[0][1]),'ask_sz':float(a[0][1]),'bids':b,'asks':a}
def recent_trades(inst):
 d=get(f"{OKX}/api/v5/market/trades?{urlencode({'instId':inst,'limit':100})}");rows=d.get('data') or [];buy=sell=0.0
 for x in rows:
  try:
   n=float(x['px'])*float(x['sz'])
   if str(x.get('side')).lower()=='buy':buy+=n
   else:sell+=n
  except Exception:pass
 total=buy+sell
 return {'samples':len(rows),'buy_notional':buy,'sell_notional':sell,'signed_imbalance':(buy-sell)/total if total>0 else None,'buy_share':buy/total if total>0 else None}
def oticker(inst):
 d=get(f"{OKX}/api/v5/market/ticker?instId={inst}")['data'][0];return {'bid':float(d['bidPx']),'ask':float(d['askPx']),'last':float(d['last'])}
def funding(inst):
 d=get(f"{OKX}/api/v5/public/funding-rate?instId={inst}");x=d['data'][0] if d.get('data') else {};return float(x.get('fundingRate') or 0)
def oi(inst):
 d=get(f"{OKX}/api/v5/public/open-interest?{urlencode({'instType':'SWAP','instId':inst})}");x=d['data'][0] if d.get('data') else {};return float(x.get('oiUsd') or x.get('oiCcy') or x.get('oi') or 0)
def depth_imb(book):
 def notion(rows):return sum(float(x[0])*float(x[1]) for x in rows[:20])
 b=notion(book.get('bids',[]));a=notion(book.get('asks',[]));return b/(a+b) if a+b>0 else None
def top_imb(book):
 b=float(book.get('bid_sz') or 0);a=float(book.get('ask_sz') or 0);return b/(a+b) if a+b>0 else None
def microprice(book):
 b=float(book['bid']);a=float(book['ask']);bs=float(book['bid_sz']);asz=float(book['ask_sz']);den=bs+asz
 return (a*bs+b*asz)/den if den>0 else (a+b)/2
def net_cross(b,o):
 x=(o['bid']/b['ask']-1)-2*(TAKER+SLIP);y=(b['bid']/o['ask']-1)-2*(TAKER+SLIP);return {'buy_binance_sell_okx':x,'buy_okx_sell_binance':y,'best':max(x,y)}
def tri():
 btc=bbook('BTCUSDT');eth=bbook('ETHUSDT');eb=bbook('ETHBTC');f=(1-TAKER-SLIP)
 c1=(1/btc['ask'])/eb['ask']*eth['bid']*(f**3)-1;c2=(1/eth['ask'])*eb['bid']*btc['bid']*(f**3)-1
 return {'USDT_BTC_ETH_USDT':c1,'USDT_ETH_BTC_USDT':c2,'best':max(c1,c2)}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--state-dir',default='strategy_atlas_microstructure_runtime');args=ap.parse_args();safe=assert_safe_startup();root=Path(args.state_dir);root.mkdir(parents=True,exist_ok=True);now=datetime.now(timezone.utc).isoformat();rows=[];errors={}
 for s in SYMS:
  try:
   bb=bbook(f'{s}USDT');ob=obook(f'{s}-USDT');spot=oticker(f'{s}-USDT');swap=oticker(f'{s}-USDT-SWAP');flow=recent_trades(f'{s}-USDT');fr=funding(f'{s}-USDT-SWAP');openi=oi(f'{s}-USDT-SWAP');cross=net_cross(bb,ob);basis=swap['last']/spot['last']-1;imb=depth_imb(ob);timb=top_imb(ob);mp=microprice(ob);mid=(ob['bid']+ob['ask'])/2;mp_edge=(mp/mid-1) if mid>0 else None;spread=(spot['ask']/spot['bid']-1) if spot['bid']>0 else None
   rows.append({'symbol':s,'binance':bb,'okx_spot':spot,'cross_exchange':cross,'spot_perp_basis':basis,'funding_rate':fr,'open_interest':openi,'okx_orderbook_imbalance':imb,'okx_top_level_imbalance':timb,'okx_trade_flow':flow,'okx_microprice':mp,'okx_microprice_edge':mp_edge,'okx_spot_spread':spread,'flags':{'cross_exchange_net_positive':cross['best']>0,'basis_abs_gt_30bps':abs(basis)>.003,'funding_abs_gt_5bps':abs(fr)>.0005,'orderbook_imbalance_extreme':imb is not None and (imb>.68 or imb<.32),'trade_flow_imbalance_extreme':flow.get('signed_imbalance') is not None and abs(flow['signed_imbalance'])>=.35,'microprice_edge_gt_1bp':mp_edge is not None and abs(mp_edge)>=.0001,'passive_spread_gt_cost_proxy':spread is not None and spread>2*.0002}})
  except Exception as e:errors[s]=f'{type(e).__name__}: {e}'
 try:t=tri()
 except Exception as e:t={'error':f'{type(e).__name__}: {e}'}
 snap={'schema':'TRADINGCORE_STRATEGY_ATLAS_MICROSTRUCTURE_V2','recorded_at_utc':now,'protocol_version':PROTOCOL_VERSION,'protocol_fingerprint':PROTOCOL_FINGERPRINT,'families':list(MICROSTRUCTURE_FAMILIES),'symbols':rows,'triangular':t,'errors':errors,'safety':safe,'private_api_used':False,'real_orders_enabled':False,'live_permission':False,'note':'Forward feature snapshot only. Predictive signals still require chronological forward validation after costs/latency.'}
 atomic(root/'LATEST_MICROSTRUCTURE_SNAPSHOT.json',snap);journal=root/'MICROSTRUCTURE_SNAPSHOTS.jsonl';old=journal.read_text(encoding='utf-8').splitlines()[-1499:] if journal.exists() else [];old.append(json.dumps(snap,separators=(',',':'),default=str));journal.write_text('\n'.join(old)+'\n',encoding='utf-8');print('ATLAS_MICRO_V2',now,'tri_best=',t.get('best'),'flags=',sum(sum(1 for v in x['flags'].values() if v) for x in rows),'errors=',errors,'real_orders=False');return 0
if __name__=='__main__':raise SystemExit(main())
