# TradingCore — Final Report

**Date:** 2026-07-28 · **Mode:** paper/demo only — no real orders, no mainnet, no leverage, no real money at any point.

---

## Headline

The **infrastructure** is now sound, tested, and honest. The **strategies are not viable**. Both ORB and Session VWAP Trend Pullback measure as **FRAGILE** against real historical data, and neither is cleared for paper-forward or demo. Gate B (Backtest Validity) **fails**, which blocks every downstream gate.

This is the most important finding of the entire effort, and it only became knowable *because* the backtest engine was built and proven correct.

---

## 1. Tests

| | Result |
|---|---|
| **Passed** | **225** |
| **Failed** | **0** |
| **Skipped** | **0** (no hidden skips, no xfail, no weakened assertions) |
| Session start | 67 |

Breakdown: unit 171 · integration 29 · regression 16 · e2e 9. Full inventory in `TEST_RESULTS.md` and `AUTOTRADING_TEST_MATRIX.md`.

**Tests were proven to work by breaking the code on purpose:**

| Injected bug | Caught by |
|---|---|
| `visible_market` → `market` in ORB retest/entry | 2 tests failed (`assert 105.0 == 100.25`) |
| Backtest context exposes full market | 3 tests failed, incl. *past decisions changed*: `[107.9,...] != [104.9,...]` |

---

## 2. Critical blockers

| # | Blocker | Evidence |
|---|---|---|
| **C-1** | **Neither strategy has an edge** | ORB: n=7, net **+0.37** vs **2.98 in fees**, FRAGILE under 2× fees. VWAP: n=98, net **−128.37 (−12.8%)**, profit factor **0.092**, **0%** walk-forward consistency, 13 consecutive losses. |
| **C-2** | **No scheduler loop exists** | `/paper/tick` is manual-trigger only. `ExitMonitor` is never called automatically — an opened paper position would never close. Makes Gates D/E/F structurally impossible. |

Full list: `ISSUES.md` (2 critical, 6 high, 7 medium, 4 low).

---

## 3. Readiness

| Track | Readiness | Why |
|---|---|---|
| **Paper trading** | **55%** | Engine correct and tested; no continuous loop; no viable strategy; dormant risk limits |
| **Bybit Demo** | **35%** | REST adapter complete + safety-enforced; **no WebSocket**; **never connected to the real API**; no credentials |
| **Real money** | **0% — 🔒 BLOCKED** | Gate G locked; **no live-order code exists anywhere in the repo** |

---

## 4. Look-ahead bias — resolved and guarded

Two real look-ahead defects were found and fixed (ORB retest/entry reading untruncated market; backtest needed strict truncation). Nine tests now guard against regression, including the strictest form: *appending future candles must not change past decisions*. Verified by fault injection, not assumption.

The backtest additionally enforces: decision on closed candle → **entry fills on the next candle's open**, exit never on the entry candle, and **SL+TP in one candle resolves to STOP** — the profitable outcome is never auto-selected.

---

## 5. Paper simulator realism — honest assessment

**Realistic:** fees both sides, adverse slippage, partial fills, balance constraints (no leverage possible), realized PnL against weighted-average entry, atomic restart persistence, deterministic replay, append-only audit ledger.

**Not realistic:** no order book, no bid/ask (spread is a flat assumption), no network latency, no gap-through-stop modeling, no rejected/delayed fills from an exchange's perspective, no funding.

---

## 6. Risk engine — correct, but partly dormant

**Working and tested:** 0.1% risk per trade sized from *real* entry-to-stop distance (not raw ATR); fees, slippage, tick size, lot size, min notional; NaN/Inf/negative/zero rejected on every input (Hypothesis property, 200 examples); no-leverage cap; one open position; duplicate/session dedup; daily trade + risk caps; R:R ≥ 2 enforced before order creation; kill switch fails closed.

**Implemented but NOT wired in (inert today):** `LossStreakGuard` — cooldown after loss, consecutive-loss limit, max-drawdown stop, max-trades-per-session. `DecisionEngine` never calls it. Also, there is no realized-PnL daily *loss* limit — the daily guard tracks planned risk only.

---

## 7. Backtest validation

| Strategy | Trades | Net PnL | Walk-forward | Stress | Verdict |
|---|---|---|---|---|---|
| ORB | 7 | +0.37 (fees 2.98) | 57.1% | negative at 2× fees, 2×/3× slippage, all-costs×2 | **FRAGILE** |
| VWAP | 98 | −128.37 | **0.0%** | negative in every scenario | **FRAGILE — clearly losing** |

Data: BTCUSDT 5m, 11,999 closed candles, ~6 weeks. **No parameter optimization was performed** — deliberately, since tuning on this window would manufacture overfitting. Details: `AUTOTRADING_BACKTEST_REPORT.md`.

**No profitability claim is made.** ORB's n=7 is statistically meaningless; VWAP's n=98 is sufficient to reject it.

---

## 8. Exact commands

```bash
# Validate configuration (no connection, no secrets printed)
curl http://localhost:8000/safety
curl http://localhost:8000/demo/preflight

# Start paper mode
.venv/Scripts/python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000
curl "http://localhost:8000/paper/tick"     # manual trigger — no auto loop yet

# Tests
.venv/Scripts/python.exe -m pytest tests/ -q

# Backtest validation
.venv/Scripts/python.exe scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 12000
.venv/Scripts/python.exe scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy orb

# Bybit Demo — NOT AUTHORIZED YET (Gate B fails, Gates C-F incomplete)
TRADING_ENVIRONMENT=DEMO .venv/Scripts/python.exe -m uvicorn api.server:app --port 8000

# Kill switch
curl -X POST "http://localhost:8000/kill-switch/engage?reason=<why>"
curl -X POST "http://localhost:8000/kill-switch/disengage"
```

---

## 9. Known limitations

- No background scheduler → no continuous operation, exits never auto-trigger.
- Bybit demo adapter **never contacted the real API**; all 23 tests mocked; schema assumptions from docs.
- **No WebSocket client** — spec required WS + reconnect + REST fallback; only REST exists.
- Backtest bypasses `PaperBroker` and `DecisionEngine`, so its numbers exclude daily limits, kill switch, and R:R gating.
- Class-level state is **not multi-process safe** — `--workers 2` would silently duplicate limits.
- VWAP session recalculation is O(n²); VWAP backtest numbers predate the regime filters.
- ORB retest is degenerate (breakout and retest key off the same candle).
- Single symbol, single ~6-week regime window; no bull/bear/high-vol separation.
- `python-dotenv` declared but never imported — `.env` is documented but not auto-loaded.

---

## 10. Required observation periods

| Stage | Minimum |
|---|---|
| Paper forward | 72h continuous, ≥ 20 trades |
| Extended demo | 2+ weeks, ≥ 30 trades |

At the current ORB rate (7 trades / 6 weeks), reaching 20–30 trades would take **months** — itself an argument for a different timeframe or strategy.

---

## 11. Five next actions

1. **Decide the strategies' fate** — move to a higher timeframe where the edge exceeds ~0.2% round-trip cost, fix the degenerate retest, or reject these formulations. Do not parameter-sweep the existing window.
2. **Build the scheduler loop** — `tick → exit check → reconcile → heartbeat` per closed candle.
3. **Route the backtest through `PaperBroker`** so replay and live share one execution path.
4. **Wire `LossStreakGuard` into `DecisionEngine`** and add a realized-PnL daily loss limit.
5. **Make state multi-process safe**, then connect Bybit demo and build the WebSocket client.

---

## 12. Gate G — real money

🔒 **REMAINS LOCKED.** Gates: A ✅ pass · B ❌ **fail** · C 🟡 partial · D ⛔ not started · E ⛔ blocked · F ⛔ not started · G 🔒 locked.

`TRADING_ENVIRONMENT=LIVE` raises `ConfigurationError`. **No code capable of placing a real order exists in this repository.** Reaching Gate G would require writing new code subject to separate review and your explicit authorization — plus a separate production key with withdrawals disabled, IP restrictions, an explicit micro-live config, 0.1% risk, no leverage, a configured max initial exposure, and a kill-switch test immediately beforehand. **It is not a configuration flip.**

---

## 13. Repository state

- **38 new files**, 7 modified. Nothing committed, pushed, or merged — all changes are in the working tree for review via `git diff` / `git status`.
- No destructive git operations. No background processes left running.
- No secrets in the repo (automated test enforces this). `.env.example` only; `.env` is gitignored.
- Deliverables: `AUTOTRADING_PRODUCTION_READINESS.md`, `AUTOTRADING_NEXT_ACTIONS.md`, `AUTOTRADING_BACKTEST_REPORT.md`, `AUTOTRADING_PAPER_REPORT.md`, `AUTOTRADING_DEMO_SETUP.md`, `AUTOTRADING_RISK_REGISTER.md`, `AUTOTRADING_TEST_MATRIX.md`, `AUTOTRADING_RELEASE_GATES.md`, `TEST_RESULTS.md`, `ISSUES.md`, `FINAL_REPORT.md`, `.env.example`.
