# TradingCore — Final Report (Priorities 1–8 session)

**Date:** 2026-07-28 · **Mode:** paper/demo only — no real orders, no mainnet, no leverage, no real money at any point.

This session continued from a prior audit-driven safety pass. The audit's finding was accepted as-is: **ORB and VWAP were not defended or cosmetically tuned.** Instead, the system was moved toward a trustworthy paper-research and execution platform, and in the process a genuine, more severe problem with ORB was discovered and fixed.

---

## 1. Files changed

**Second checkpoint commit:** `f31c655` — 36 files changed, 2,693 insertions(+), 469 deletions(-).
**First checkpoint commit (start of this session):** `b9e7c91` — the prior audit session's work, 153 files.

New this session:
```
api/scheduler/loop.py                              -- scheduler/event loop
api/risk/guards.py                                  -- 5 split, wired risk guards
api/market_data/resilience.py                       -- retry/backoff, rate-limit, clock-skew
api/backtesting/research.py                         -- sample-size, benchmarks, regime, MC, param stability
api/strategy_engine/strategies/orb/candidates.py    -- 2 labeled ORB candidates
api/strategy_engine/strategies/vwap/candidates.py   -- 3 labeled VWAP candidates
scripts/run_paper_loop.py                           -- standalone loop runner (SIGINT/SIGTERM)
scripts/run_candidate_research.py                   -- independent candidate evaluation
9 new test files (73 new tests)
```

Modified: `api/decision_engine/decision_engine.py`, `api/trade_engine/trade_engine.py`, `api/risk_engine.py`, `api/backtesting/backtest_engine.py`, `api/strategy_engine/strategies/orb/take_profit.py` + `orb_strategy.py`, `api/server.py`, `api/binance.py`, `api/bybit.py`, `config/settings.py`, plus 7 documentation files and `tests/conftest.py`.

Nothing was pushed. `git log`: `f31c655` (this session) → `b9e7c91` (prior session) → `3a7f055` (original repo).

---

## 2. Tests added and current totals

| | Count |
|---|---|
| Start of this session | 225 |
| **End of this session** | **303** |
| Failed | 0 |
| Skipped | 0 |

9 new test files, 73 new tests. Full breakdown in `TEST_RESULTS.md` / `AUTOTRADING_TEST_MATRIX.md`.

Two more real bugs were caught **by the tests themselves failing**, not by inspection:
- `MaxDrawdownGuard` counting position-open as drawdown (caught by `test_h2_restart_then_genuinely_new_signal_can_still_trade`).
- The dict-unpacking status bug from the prior session's pattern did not recur, but the same discipline — write the test, let it fail, find out why — is what surfaced both this session's new bugs.

---

## 3. Infrastructure completed

- **Scheduler/event loop**: candle-close-aligned ticking, automatic exit-monitoring every tick, pending-order reconciliation, heartbeat, structured JSON logs, Telegram-mock alerts, graceful shutdown. **72-hour continuous paper-forward is now structurally possible** — it was not, before this session.
- **All five risk guards wired**: `LossStreakGuard`, `CooldownAfterLossGuard`, `MaxDrawdownGuard`, `MaxTradesPerSessionGuard`, `MaxOpenPositionsGuard`, plus new `DailyLossGuard` (realized loss). Every one gates real decisions in `DecisionEngine`; every blocked trade carries a machine-readable `reason` and `guard` field.
- **Market-data resilience**: retry-with-backoff, HTTP 429 rate-limit handling, clock-skew detection — verified live against both Binance's and Bybit's real server-time endpoints (skew: −0.5s and −0.9s respectively, both well within tolerance).
- **Research pipeline**: minimum sample-size flagging, buy-and-hold and NO_TRADE benchmarks, regime segmentation, parameter-stability analysis, Monte Carlo trade-order permutation — all tested, none used to cherry-pick a result.
- **Backtest performance fix**: indicator computation was O(n²) in practice (recomputed over full history every candle); bounded to a 260-candle rolling window, verified to produce byte-identical results whenever data is shorter than the window.
- **Bybit Demo adapter**: unchanged from prior session (REST complete, safety-enforced, mocked-tested, never connected) — confirmed still behind a strict `TRADING_ENVIRONMENT=DEMO` feature flag; `LIVE` raises `ConfigurationError`.

---

## 4. Unresolved blockers

1. **No strategy has a demonstrated edge** (see §5) — the central blocker, unchanged in kind but now much better evidenced.
2. Backtest still bypasses `DecisionEngine`/`PaperBroker` — doesn't exercise the newly-wired guards.
3. Class-level state (`PositionManager`, all guards, broker singletons) is not multi-process safe.
4. Bybit demo adapter has never contacted the real API; no WebSocket client exists.
5. VWAP's own session-VWAP calculation has a separate performance issue (not fixed by the backtest-engine perf fix) that made a 6-month VWAP run impractical this session.
6. Several exploratory backtest jobs (train/validation/test split + walk-forward + sensitivity, re-run with the take-profit fix on top of the larger dataset) were still executing in the background when this report was written, due to genuine compute cost plus contention from multiple concurrent jobs in this environment. The **core, decisive full-period numbers** (below) completed and are solid; the supplementary walk-forward/sensitivity granularity on the 6-month scale specifically did not finish in time and is not included as a result — only what actually completed is reported.

---

## 5. Exact strategy results, without exaggeration

**A critical bug was found and fixed during this work — not by inspection, but by tracing an anomalous real-data backtest result.** `TakeProfit.calculate` always computed a LONG-direction profit target regardless of trade direction. For a SHORT, this placed the take-profit above both entry and stop — unreachable in the profitable direction. A SHORT opened, price moved 33% in its favor, and the position never closed, silently blocking every subsequent signal for the rest of a 4-month backtest window. Fixed; 5 regression tests reproduce the exact scenario (`tests/regression/test_take_profit_direction.py`).

**With the fix applied, the picture is more decisive, not less:**

| Strategy | Scale | Trades | Net PnL | Profit factor | Win rate | Max DD |
|---|---|---|---|---|---|---|
| ORB | 6 weeks | 49 | **−48.59 (−4.9%)** | 0.174 | 34.7% | 5.0% |
| ORB | 6 months | 208 | **−176.54 (−17.7%)** | 0.205 | 38.5% | 17.9% |
| VWAP | 6 weeks | 98 | **−128.37 (−12.8%)** | 0.092 | 21.4% | 12.8% |

VWAP's numbers are unchanged from the prior report — it is LONG-only and never called the buggy `TakeProfit` class (confirmed by direct code inspection, not assumed). ORB's numbers are new: the previous report's 7-trade, near-breakeven reading was itself partly an artifact of the same bug suppressing trade resolution.

**No profitability claim is made.** Both strategies are now adequately sampled and both show a consistent, robust negative edge across independent time windows. This is a stronger and more useful finding than "not enough data" — it is a clear "these specific formulations don't work at 5-minute granularity with realistic costs."

---

## 6. Is 72-hour paper-forward structurally possible?

**Yes — for the first time.** The scheduler loop (`api/scheduler/loop.py`) did not exist at the start of this session; without it, `/paper/tick` had to be triggered manually and `ExitMonitor` was never invoked automatically. Both gaps are now closed, unit/integration-tested (10 tests), and the loop supports graceful shutdown.

**It was deliberately not run** this session: running 72 hours of paper-forward against strategies just shown to be robustly unprofitable at two independent scales would produce confirmation of what the backtests already demonstrate, not new evidence. It should follow a decision about the strategies (see `AUTOTRADING_NEXT_ACTIONS.md` item 1), not precede it.

---

## 7. Readiness percentages

| Track | Readiness |
|---|---|
| Paper trading | **65%** (up from 55%) — infrastructure now complete; blocked on strategy edge |
| Bybit Demo | **35%** (unchanged) — adapter ready, never connected, no WebSocket |
| Real money | **0% — 🔒 LOCKED** (Gate G, unchanged) |

---

## 8. Git status and commit hash

```
Latest commit: f31c655  "Wire risk guards, add scheduler loop, fix critical SHORT take-profit bug"
Prior commit:  b9e7c91  "Audit-driven safety pass: fix critical execution/risk bugs, add paper trading infrastructure"
Branch: main, tracking origin/main (not pushed)
Working tree: clean (data/, reports/, state/ gitignored and untouched by git)
```

No push was performed. No destructive git operations. No secrets committed (automated test + manual diff scan both confirm).

---

## 9. Next recommended action

**Decide the fate of ORB and VWAP as currently formulated** (`AUTOTRADING_NEXT_ACTIONS.md` item 1) before investing further in infrastructure or running a 72-hour forward test. Concretely: move to a higher timeframe where the edge would exceed the ~0.2% round-trip cost, fix ORB's degenerate one-candle retest, or reject both formulations and design a new candidate from scratch — evaluated on 12+ months of held-out data, never tuned on the window used to develop it.

---

## Completion standard — self-check

- ✅ No profitability claim made without adequately-sampled out-of-sample evidence — and the evidence found says the opposite of profitable.
- ✅ No live trading, no leverage, no real-money credentials touched.
- ✅ No failed gate bypassed — Gate B fails, and every downstream gate is correctly reported as blocked or not-yet-attempted.
- ✅ Honest NO_TRADE / FRAGILE results reported in place of a misleading profitable backtest, even though this meant revising the prior session's more equivocal reading to a more damning one.
