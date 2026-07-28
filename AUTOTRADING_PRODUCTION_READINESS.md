# TradingCore — Production Readiness

**Assessed:** 2026-07-28 · **Tests:** 225 passed / 0 failed / 0 skipped

| Track | Readiness | Status |
|---|---|---|
| **Paper trading** | **55%** | Infrastructure sound; no continuous loop; no viable strategy |
| **Bybit Demo** | **35%** | REST adapter complete + safety-enforced; never connected; no WebSocket |
| **Real money** | **0%** | 🔒 **BLOCKED — Gate G locked, no live order code exists** |

---

## 1. What is genuinely production-grade now

| Component | Evidence |
|---|---|
| No-look-ahead guarantees | Fault-injection verified — 3 tests fail when leakage is introduced |
| Deterministic backtest engine | Costs modeled; entry on next candle's open; SL+TP→stop; unresolved trades contribute zero PnL |
| Position sizing | Fees, slippage, tick/lot size, min notional, no-leverage cap; NaN/Inf/negative rejected; 200-example Hypothesis property |
| Order idempotency & reconciliation | Deterministic client IDs; timeout never triggers blind resend; unknown state stays unknown |
| Paper broker | Fills, fees, slippage, partial fills, balance, realized PnL, restart persistence, deterministic replay |
| Exit monitor | SL / TP / invalidation / stale; **conservative same-candle rule** (never auto-selects profit); reconciles against broker |
| Kill switch | Blocks entries, preserves monitoring, cancels pending, persists across restart, **fails closed** on corruption |
| Session/timezone handling | Single source of truth; DST-correct (verified across EDT/EST and BST transitions) |
| Regime / liquidity / stale-data filters | Undetermined regime blocks trading; stale data blocks in live mode |
| Bybit demo safety | Production hosts rejected per-request; secrets never printed; retries reuse identical `orderLinkId` |
| Security hygiene | No secrets, no `eval`/`exec`/`pickle`/shell; enforced by automated test |

---

## 2. Why paper trading is 55%, not higher

| Blocker | Detail |
|---|---|
| **No scheduler loop** | `/paper/tick` must be triggered manually. Nothing calls `ExitMonitor` automatically — an opened position would never close on its own. |
| **No strategy with an edge** | ORB: +0.37 net vs 2.98 fees on n=7. VWAP: −128.37, PF 0.092, 0% walk-forward consistency. Both **FRAGILE**. |
| **Backtest bypasses the live execution path** | Validates neither `PaperBroker` nor `DecisionEngine` gates. |
| **Dormant risk limits** | `LossStreakGuard` (cooldown, consecutive losses, drawdown stop, per-session cap) implemented + tested but never called. |
| **No realized-PnL daily loss limit** | Daily guard tracks *planned* risk, not realized losses. |
| **Not multi-process safe** | Class-level state; `--workers 2` would silently duplicate limits. |

## 3. Why Bybit demo is 35%

| Present | Missing |
|---|---|
| REST: create/amend/cancel/query, executions, position, balance, klines | **WebSocket client entirely absent** (spec required WS + reconnect + REST fallback) |
| Endpoint validation rejecting production hosts | **Never connected to the real API** — schema assumptions from docs, not traffic |
| Credential validation without leakage | No credentials supplied (by design) |
| Rate-limit backoff, retry with idempotency | No live rate-limit behavior observed |
| 23 mocked tests | Zero integration tests against real demo |

---

## 4. Safety posture (verified)

```
GET /safety
{"paper_trading": true, "live_trading": false,
 "live_order_code_present": false, "kill_switch_engaged": false}
```

- `TRADING_ENVIRONMENT=LIVE` raises `ConfigurationError` — there is **no live order code path** anywhere in the repository.
- Default risk 0.1% per trade, leverage 1, spot long-only, no averaging down, no martingale.
- Every failure path defaults to `NO_TRADE` or `FAILED_SAFELY`.
- Corrupted kill-switch state fails **closed** (engaged), never open.

---

## 5. Exact commands

**Validate configuration (no connection, no secrets printed):**
```bash
curl http://localhost:8000/safety
curl http://localhost:8000/demo/preflight
```

**Start paper mode:**
```bash
.venv/Scripts/python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000
# then trigger ticks manually (no automatic loop exists yet):
curl "http://localhost:8000/paper/tick"
```

**Run tests:**
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

**Run backtest validation:**
```bash
.venv/Scripts/python.exe scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 12000
.venv/Scripts/python.exe scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy orb
```

**Start Bybit Demo** — *not authorized yet; Gate B fails and Gates C–F are incomplete:*
```bash
# Only after Gates A–D pass and you have supplied demo credentials in .env
TRADING_ENVIRONMENT=DEMO .venv/Scripts/python.exe -m uvicorn api.server:app --port 8000
```

**Kill switch:**
```bash
curl -X POST "http://localhost:8000/kill-switch/engage?reason=<why>"
curl http://localhost:8000/kill-switch/status
curl -X POST "http://localhost:8000/kill-switch/disengage"
```

---

## 6. Required observation periods (once gates permit)

| Stage | Minimum | Rationale |
|---|---|---|
| Paper forward | **72 hours continuous**, ≥ 20 trades | Detect stuck positions, reconciliation drift, restart bugs |
| Extended demo | **2+ weeks**, ≥ 30 trades | Statistically meaningful; PnL must match exchange reporting |
| Pre-live review | Separate explicit decision | Gate G — see below |

At current trade frequency (7 ORB trades per 6 weeks), reaching 20–30 trades would take **months**. This is itself an argument for a higher-frequency timeframe or a different strategy formulation.

---

## 7. Gate G — real money

🔒 **LOCKED.** This work does not and cannot unlock it. Requires: all of Gates A–F passing (currently A passes, B **fails**, C partial, D/E/F not started), explicit user approval, a separate production key with withdrawals disabled and IP restrictions, an explicit micro-live config, risk held at 0.1%, no leverage, a configured maximum initial exposure, and a kill-switch test immediately beforehand.

Reaching it would require **writing new code that does not exist**, subject to separate review and authorization. It is not a configuration flip.
