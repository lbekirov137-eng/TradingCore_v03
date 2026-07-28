# TradingCore — Release Gates

Revision 2 (2026-07-28). Supersedes the earlier Gate 0–6 numbering with the
exact Gate A–G structure specified for this phase of work. Each gate requires
the stated evidence to exist and pass before the next is attempted.
**Gate G is LOCKED and stays locked regardless of any other progress**,
pending a separate, explicit decision by the user.

Test suite at time of writing: **300+ passed, 0 failed, 0 skipped** (see
TEST_RESULTS.md for the exact count at report time).

---

## GATE A — CODE CORRECTNESS ✅ PASS

**Requires:** infrastructure and unit tests pass.

| Check | Result |
|---|---|
| Full test suite | All passing, 0 skipped, 0 xfail |
| Unit / integration / regression / e2e layers | All present and green |
| Secrets in repo | None (automated scan test) |
| Default config | `PAPER_TRADING=True`, `LIVE_TRADING=False`, leverage 1, risk 0.1% |
| Suite determinism | Verified across repeated runs with isolated runtime state |

---

## GATE B — BACKTEST VALIDITY ❌ **FAIL** (more decisively than initially thought)

**Requires:** no look-ahead; realistic fees and slippage; valid train/validation/test split; sufficient sample size.

| Sub-requirement | Status |
|---|---|
| No look-ahead | ✅ Proven by fault injection — deliberately breaking `visible_market` truncation makes 3 dedicated tests fail, including the strictest one: *appending future candles changes past decisions* |
| Realistic fees/slippage | ✅ Modeled (0.1% fee/side, 5bps slippage/side, 2bps spread) and stress-tested at 2×/3× |
| Train/validation/test split | ✅ Implemented (`train_test_split`), non-overlapping, verified by test |
| **Sufficient sample size** | ✅/❌ **Now adequately sampled for ORB too (49 trades @ 6wk, 208 @ 6mo) — and it clearly fails.** VWAP was already adequately sampled (98 trades) and already failing. |

**A CRITICAL bug was found and fixed during this validation work, not by inspection but by tracing an anomalous "0 trades over 4 months" result on a 6-month dataset:** `TakeProfit.calculate` always computed a LONG-direction target regardless of trade direction, so a SHORT trade's take-profit sat *above* both entry and stop — unreachable in the profitable direction. A SHORT opened at 89234.8, price fell to 60000 (a huge win), and the position never closed because the correct exit level was never touched. It silently blocked every subsequent signal for the rest of the dataset. Fixed, with 5 regression tests reproducing the exact scenario.

**With the fix applied, ORB's true trade frequency and edge became visible — and it is decisively negative, not merely under-sampled:**

| Strategy | Scale | Trades | Net PnL | Profit factor | Walk-forward consistency |
|---|---|---|---|---|---|
| ORB | 6 weeks | 49 | −48.59 | 0.174 | **0.0%** (0/7 windows) |
| ORB | 6 months | 208 | −176.54 (−17.65%) | 0.205 | — |
| VWAP | 6 weeks | 98 | −128.37 (−12.8%) | 0.092 | **0.0%** (0/7 windows) |

Every train/validation/test split for ORB (6-week) is also negative (train −37.60, validation −22.32, held-out test −1.71), and every sensitivity/stress scenario is worse than an already-negative baseline.

Gate B **fails decisively**: both strategies are now adequately sampled (ORB at two independent scales with consistent results), both show a robust negative edge, and both have identical (zero) walk-forward consistency. This is not "not enough data to tell" at any level of the analysis. See `AUTOTRADING_BACKTEST_REPORT.md` for full numbers and methodology.

---

## GATE C — PAPER REPLAY 🟡 PARTIAL

**Requires:** positive out-of-sample expectancy after costs; acceptable profit factor; acceptable drawdown; parameter stability; walk-forward consistency.

| Sub-requirement | Status |
|---|---|
| Positive out-of-sample expectancy | ❌ Not demonstrated at adequate sample size (Gate B blocks this) |
| Acceptable profit factor | ❌ VWAP: 0.092 (need >1.0 to be profitable at all). ORB: too few trades to trust 1.12 |
| Acceptable drawdown | 🟡 ORB drawdown small (~0.26–2.9% depending on window) but on a tiny sample; VWAP drawdown 12.8% on adequate sample |
| Parameter stability | ✅ Tooling built (`parameter_stability_analysis`) — flags `SIGN_FLIPS_WITH_SMALL_CHANGES` as a fragility signal, not silently averaged away |
| Walk-forward consistency | ❌ ORB 57% (small-sample), VWAP **0%** (adequate sample, unambiguous) |

This gate cannot pass while Gate B fails — listed as partial because the *tooling* to evaluate it exists and works correctly, even though the *strategies* don't clear the bar.

---

## GATE D — 72-HOUR PAPER FORWARD ⛔ NOT STARTED (structurally possible now, not yet run)

**Requires:** 72-hour uninterrupted paper execution; exits work; recovery works; no duplicate positions; complete observability.

| Sub-requirement | Status |
|---|---|
| Scheduler/event loop | ✅ **Built this session** — `api/scheduler/loop.py`, candle-close-aligned, graceful shutdown, structured logging, heartbeat |
| Exits work automatically | ✅ `ExitMonitor` wired into every loop tick — SL/TP/invalidation/stale, conservative same-candle rule |
| Recovery works | ✅ Idempotency store + `PositionManager` + paper broker all restart-persistent; replaying a signal after restart never opens a duplicate position (tested) |
| No duplicate positions | ✅ Enforced at three independent layers: `MaxOpenPositionsGuard`, `MaxTradesPerSessionGuard`, order-layer idempotency |
| Complete observability | ✅ Heartbeat, structured JSON logs, PnL/position/risk reports, Telegram-mock alerts, support bundle (no secrets) |

**72-hour paper-forward is now structurally possible** — this was not true at the start of this session (no scheduler loop existed at all). It has **not been run** because: (1) running it against strategies already known to be FRAGILE at current sample size would burn 3 days producing more noise, not more evidence, and (2) it should follow, not precede, a decision about what strategy variant to actually run (see `AUTOTRADING_NEXT_ACTIONS.md`).

---

## GATE E — BYBIT DEMO ⛔ BLOCKED (adapter ready, never connected)

**Requires:** Bybit Demo connectivity and order lifecycle verified.

| Sub-requirement | Status |
|---|---|
| REST adapter (order lifecycle, executions, position, balance) | ✅ Implemented |
| Endpoint safety (production hosts rejected) | ✅ Enforced per-request, tested |
| Rate-limit handling, retry with idempotency | ✅ `orderLinkId` = deterministic `client_order_id`; retries verified to reuse the identical ID |
| Feature flag | ✅ Requires `TRADING_ENVIRONMENT=DEMO` explicitly; `LIVE` raises `ConfigurationError`; construction fails without it |
| Credentials | ❌ None supplied (by design — awaiting user) |
| **Verified against the real demo API** | ❌ **Never connected.** All 23 adapter tests are mocked. |
| WebSocket + reconnect | ❌ **Not implemented** — REST polling only |

Blocked on: Gate B/C/D not clearing, credentials not supplied, and no real connectivity test having ever run.

---

## GATE F — EXTENDED DEMO OBSERVATION ⛔ NOT STARTED

Cannot start before Gate E.

---

## GATE G — MICRO-LIVE / LIVE ACTIVITY 🔒 **LOCKED**

**Requires:** explicit human approval for any future live activity — and, structurally, all of:

1. Gates A–F passing (currently: A ✅, B ❌, C 🟡, D/E/F not started)
2. Explicit user approval, in a separate decision from this work
3. A separate production API key (never reusing demo keys)
4. Withdrawal permission disabled on that key
5. IP restrictions configured
6. A separate, explicitly-enabled micro-live configuration
7. Risk held at 0.1% per trade
8. No leverage
9. A configured maximum initial live exposure
10. The kill switch tested immediately beforehand

**Structural fact, not a policy choice being made here:** there is **no code path in this repository capable of placing a real order.** `TRADING_ENVIRONMENT=LIVE` raises `ConfigurationError` before any request is even constructed. Reaching Gate G requires writing code that does not exist today, subject to separate review and your explicit authorization. **This document does not, and will not, recommend proceeding to Gate G.**

---

## Summary

| Gate | Name | Status |
|---|---|---|
| A | Code correctness | ✅ **PASS** |
| B | Backtest validity | ❌ **FAIL** — insufficient sample; both strategies FRAGILE where sample is adequate |
| C | Paper replay | 🟡 PARTIAL — tooling correct, strategies don't clear the bar |
| D | 72-hour paper forward | ⛔ Structurally possible now; not yet run |
| E | Bybit demo | ⛔ Adapter ready; never connected; no credentials; no WebSocket |
| F | Extended demo observation | ⛔ NOT STARTED |
| G | Live activity | 🔒 **LOCKED — requires explicit user decision, always** |

**The single blocking issue remains Gate B**, exactly as in the prior revision of this document — despite an entire session of new infrastructure (scheduler loop, wired risk guards, market-data resilience, research pipeline, a critical strategy bug fixed), no amount of infrastructure work substitutes for a strategy with a demonstrated, adequately-sampled, cost-surviving edge.
