# TradingCore Autonomous Milestone — 2026-08-15

Status captured after successful FAST TRACK V4.1 runtime validation.

## Runtime
- Main TradingCore PAPER: running, real orders disabled.
- BTC 1H Forward Shadow: running.
- Collector B (V1 BTC/ETH/SOL liquidation cohort): running.
- V1 Forced Flow Autonomous Orchestrator: running.
- V1 Forward PAPER worker: running, inert until historical PASS.
- Collector C Wide: running, 20-symbol frozen universe.
- Wide V2 Forced Flow Orchestrator: running.
- Wide V2 Forward PAPER worker: running, inert until historical PASS.
- Future Wide tasks use hidden WScript launchers.
- LIVE / real orders remain disabled.

## Latest observed gates from owner console
### V1
- G2: G2_PASS
- G3: G3_PENDING_SAMPLE
- valid events: 8
- research state: waiting for preregistered sample

### Wide V2
- frozen universe: 20 symbols
- universe fingerprint: 04b6034badcc01b9e5e84f23158f659d4a512822444d34d787fc8ce4758c8951
- G2: G2_PASS
- G3: G3_PENDING_SAMPLE
- valid events: 7
- research state: WAITING_FOR_WIDE_PREREGISTERED_SAMPLE
- forward state: WAITING_HISTORICAL_RESEARCH_PASS

## V1 preregistered research gate
From `forced_flow_protocol.py`:
- minimum valid events: 1,000
- minimum observation span: 72 hours
- minimum primary liquidation clusters: 300
- final holdout: 30%
- historical PASS cannot authorize LIVE; forward PAPER is mandatory.

## Wide V2 research gate
From `forced_flow_wide_protocol.py`:
- minimum valid events: 1,500
- minimum observation span: 72 hours
- minimum primary clusters: 300
- minimum primary symbols represented: 10
- minimum evaluable OOS symbols: 5
- minimum profitable symbol ratio: 60%
- forward PAPER minimum closed trades: 30

## Operating rule
Do not tune V1 or Wide V2 based on emerging outcomes. The preregistered protocol and frozen universe remain unchanged until the one-time historical decision. A failed protocol requires a new independent protocol/sample; it is not re-opened or weakened.

## Safety
PAPER / research only. No private exchange API keys are required by the research collectors. No real-order path is authorized by this milestone. Collector A is unchanged.
