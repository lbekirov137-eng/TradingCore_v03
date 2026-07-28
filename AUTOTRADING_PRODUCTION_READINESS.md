# TradingCore — Production Readiness

**Assessed:** 2026-07-28 (Priorities 1–8 session) · **Tests:** 303 passed / 0 failed / 0 skipped

| Track | Readiness | Status |
|---|---|---|
| **Paper trading** | **65%** | Infrastructure now complete (scheduler loop, all risk guards wired); no viable strategy |
| **Bybit Demo** | **35%** | REST adapter complete + safety-enforced; never connected; no WebSocket |
| **Real money** | **0%** | 🔒 **BLOCKED — Gate G locked, no live order code exists** |

---

## 1. What changed this session

Starting point was the end of the prior audit session (225 tests, infrastructure gaps: no scheduler, no wired risk guards, no market-data resilience layer, no research tooling). This session:

- Built the **scheduler/event loop** (`api/scheduler/loop.py`) — candle-close-aligned, automatic exit-checking, reconciliation, heartbeat, structured logs, graceful shutdown. **72-hour paper-forward is now structurally possible** (was not, before).
- **Wired all five risk guards** into `DecisionEngine`/`TradeEngine`: `LossStreakGuard`, `CooldownAfterLossGuard`, `MaxDrawdownGuard`, `MaxTradesPerSessionGuard`, and a new `DailyLossGuard` (realized loss, distinct from the existing planned-risk guard). Every blocked trade now carries a machine-readable reason.
- Added **market-data resilience**: retry-with-backoff, rate-limit handling, clock-skew detection against real exchange server-time endpoints.
- Built a **research pipeline**: minimum sample-size checks, buy-and-hold/NO_TRADE benchmarks, regime segmentation, parameter-stability analysis, Monte Carlo trade-order permutation, plus labeled candidate strategy variants evaluated independently.
- Fetched **6 months of additional history** (52k candles) and found a **critical bug** while validating on it: `TakeProfit.calculate` ignored trade direction, causing SHORT positions to get permanently stuck. Fixed, with regression tests reproducing the exact incident.
- Fixed a real **O(n²) performance defect** in the backtest engine (indicators recomputed over full history every candle) — now bounded, verified to produce identical results.
- Found and fixed a bug in **my own new `MaxDrawdownGuard` wiring**: it initially measured "equity" as cash-only, so opening a position looked like an instant drawdown.

Tests: 225 → **303**.

---

## 2. The central finding got stronger, not weaker

With the take-profit bug fixed, ORB's true trading frequency became visible, and the picture is now **more decisive**:

| Strategy | Scale | Trades | Net PnL | Profit factor | Max DD |
|---|---|---|---|---|---|
| ORB | 6 weeks | 49 | **−48.59 (−4.9%)** | 0.174 | 5.0% |
| ORB | 6 months | 208 | **−176.54 (−17.7%)** | 0.205 | 17.9% |
| VWAP | 6 weeks | 98 | **−128.37 (−12.8%)** | 0.092 | 12.8% |

Both strategies are now adequately sampled (ORB at two independent scales, consistently) and both show a robust negative edge — not a small-sample artifact. This is the honest result the audit was designed to surface, and it supersedes the earlier, more equivocal 7-trade ORB reading.

---

## 3. What is genuinely production-grade now

Everything from the prior report, plus:

| Component | Evidence |
|---|---|
| Scheduler/event loop | Candle-close scheduling, auto exit-monitoring, graceful shutdown — 10 tests |
| Wired risk guards | 5 distinct guards, all gate real decisions, all machine-readable — 29 tests |
| Market-data resilience | Retry/backoff, rate-limit handling, clock-skew (verified live against Binance & Bybit) — 10 tests |
| Research pipeline | Sample-size checks, benchmarks, regime segmentation, parameter stability, Monte Carlo — 13 tests |
| Backtest performance | O(n²) → effectively O(n), correctness-preserving — 3 tests |

---

## 4. Why paper trading is 65%, not higher

| Blocker | Detail |
|---|---|
| **No strategy with an edge** | Both are now robustly, adequately-sampled FRAGILE — the central remaining blocker |
| **Backtest bypasses the live execution path** | Still validates neither `PaperBroker` nor `DecisionEngine`'s gates |
| **Not multi-process safe** | Class-level state across `PositionManager` and all guards; `--workers 2` would silently duplicate every limit |
| **VWAP's own performance issue** | `calculate_session_vwap` is still O(session-length) per call; 6-month VWAP research not yet practical |
| **72h paper-forward not yet run** | Now possible, deliberately not run against strategies already shown to be losing at two scales |

## 5. Why Bybit demo is still 35% (unchanged)

Adapter complete and safety-enforced, but never connected to the real API (all 23 tests mocked), and no WebSocket client exists — spec required WS + reconnect + REST fallback; only REST exists.

---

## 6. Safety posture (unchanged, still verified)

```
GET /safety
{"paper_trading": true, "live_trading": false,
 "live_order_code_present": false, "kill_switch_engaged": false}
```

`TRADING_ENVIRONMENT=LIVE` raises `ConfigurationError` — no live order code path exists anywhere. Default risk 0.1%/trade, leverage 1, spot long-only, no averaging down. Every failure path defaults to `NO_TRADE`/`FAILED_SAFELY`. Corrupted kill-switch state fails closed.

---

## 7. Exact commands

```bash
# Validate configuration (no connection, no secrets printed)
curl http://localhost:8000/safety
curl http://localhost:8000/demo/preflight
curl http://localhost:8000/observability/clock-skew

# Start paper mode (manual tick)
.venv/Scripts/python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000
curl "http://localhost:8000/paper/tick"

# Start the continuous paper scheduler loop (NEW this session)
.venv/Scripts/python.exe scripts/run_paper_loop.py --symbol BTCUSDT --interval 5m
# Ctrl+C for graceful shutdown

# Tests
.venv/Scripts/python.exe -m pytest tests/ -q

# Backtest validation
.venv/Scripts/python.exe scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 12000
.venv/Scripts/python.exe scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy orb

# Candidate research (independent, non-combined variants)
.venv/Scripts/python.exe scripts/run_candidate_research.py --data data/BTCUSDT_5m.json

# Kill switch
curl -X POST "http://localhost:8000/kill-switch/engage?reason=<why>"
curl -X POST "http://localhost:8000/kill-switch/disengage"
```

---

## 8. Required observation periods (once Gate B is addressed)

| Stage | Minimum |
|---|---|
| Paper forward | 72 hours continuous, ≥ 20 trades |
| Extended demo | 2+ weeks, ≥ 30 trades |

At ORB's observed 6-month rate (~208 trades / 6 months ≈ 35/month), 20 trades would take about 2-3 weeks — much more tractable than the earlier (bug-distorted) estimate of "months." This makes the mechanics of running Gate D more practical, once there's a strategy variant worth running it against.

---

## 9. Gate G — real money

🔒 **LOCKED, unchanged.** No code capable of placing a real order exists anywhere in this repository. This report does not, and will not, recommend proceeding to Gate G.
