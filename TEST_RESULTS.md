# TradingCore — Test Results

**Run date:** 2026-07-28 (Priorities 1–8 session)
**Command:** `.venv/Scripts/python.exe -m pytest tests/ -q`

```
303 passed, 1 warning in ~15-25s
```

| Metric | Value |
|---|---|
| Passed | **303** |
| Failed | **0** |
| Skipped | **0** |
| xfail / xpass | **0** |
| Errors | **0** |

**No test is skipped, disabled, or weakened.** The only warning is a third-party deprecation notice from `starlette.testclient`, unrelated to project code.

---

## Progression this session

| Checkpoint | Tests |
|---|---|
| Start of this session (end of prior audit session) | 225 |
| After Priority 2 (scheduler loop) | 240 |
| After Priority 3 (wired risk guards) | 269 |
| After Priority 4 (market data resilience) | 279 |
| After Priority 5 (research pipeline + candidates) | 300 |
| After indicator-lookback perf fix | 303 |

---

## Breakdown by directory

| Suite | Tests |
|---|---|
| `tests/unit/` | 229 |
| `tests/integration/` | 44 |
| `tests/regression/` | 21 |
| `tests/e2e/` | 9 |
| **Total** | **303** |

## New test files this session

| File | Tests | Covers |
|---|---|---|
| `tests/regression/test_take_profit_direction.py` | 5 | The critical SHORT take-profit direction bug, found via real backtest |
| `tests/integration/test_scheduler_loop.py` | 10 | Candle-close scheduling, exit-monitor auto-invocation, graceful shutdown, error isolation |
| `tests/unit/test_risk_guards.py` | 24 | Each of the 5 new/split guard classes independently |
| `tests/unit/test_market_data_resilience.py` | 10 | Retry/backoff, rate-limit handling, clock-skew detection |
| `tests/unit/test_research_pipeline.py` | 13 | Sample-size checks, benchmarks, regime segmentation, parameter stability, Monte Carlo |
| `tests/unit/test_strategy_candidates.py` | 6 | ORB/VWAP candidate variants run independently, correctly labeled |
| Additions to `tests/unit/test_backtest_engine.py` | 5 | Time-stop feature, indicator-lookback performance fix (correctness-preserving) |
| Additions to `tests/integration/test_decision_engine.py` | 5 | Every new guard actually blocks a trade with a machine-readable reason |

---

## Fault-injection verification (carried forward + new)

| Injected bug | Tests that caught it |
|---|---|
| `visible_market` → `market` in ORB retest/entry | 2 failed |
| `BacktestContext.visible_market` exposes full market | 3 failed, incl. *past decisions changed* |
| (Real bug, not injected) TakeProfit ignoring direction | Discovered via anomalous real-data backtest result, not injection — see `AUTOTRADING_BACKTEST_REPORT.md` |
| (Real bug, not injected) MaxDrawdownGuard counting position-open as drawdown | Discovered by `test_h2_restart_then_genuinely_new_signal_can_still_trade` failing after guard wiring |

---

## What the green suite does *not* prove

- **Does not prove either strategy is profitable at adequate sample size.** VWAP is adequately sampled and unprofitable; ORB is not adequately sampled at all.
- **Does not prove the Bybit demo adapter works against the real API** — all 23 tests are mocked.
- **Does not prove 72-hour continuous stability** — the scheduler loop now exists and is unit/integration-tested, but has not been run continuously for 72 hours.
- **Does not cover multi-process deployment** — shared class-level state (guards, position manager, broker) is untested under concurrency.
