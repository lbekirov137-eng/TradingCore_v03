# TradingCore — Test Results

**Run date:** 2026-07-28
**Command:** `.venv/Scripts/python.exe -m pytest tests/ -q`

```
225 passed, 1 warning in 12.74s
```

| Metric | Value |
|---|---|
| Passed | **225** |
| Failed | **0** |
| Skipped | **0** |
| xfail / xpass | **0** |
| Errors | **0** |
| Runtime | ~12s |

**No test is skipped, disabled, or weakened.** The only warning is a third-party deprecation notice from `starlette.testclient` (suggests installing `httpx2`) — unrelated to project code.

---

## Breakdown

| Suite | Tests |
|---|---|
| `tests/unit/` | 171 |
| `tests/integration/` | 29 |
| `tests/regression/` | 16 |
| `tests/e2e/` | 9 |

### By module

| File | Tests | Focus |
|---|---|---|
| `unit/test_position_sizing.py` | 31 | Sizing incl. fees/slippage/tick/lot/min-notional + 2 Hypothesis properties |
| `unit/test_bybit_demo_adapter.py` | 23 | Demo endpoint safety, credentials, idempotency, rate limits (all mocked) |
| `unit/test_risk_engine.py` | 20 | NaN/Inf/negative rejection, daily limits, independent recalculation |
| `unit/test_regime_filters.py` | 20 | Data quality, liquidity, spread, regime classification |
| `unit/test_paper_broker.py` | 16 | Fills, partial fills, cancel/amend, persistence, determinism |
| `unit/test_backtest_engine.py` | 15 | Metrics, costs, export, walk-forward, sensitivity |
| `unit/test_candle_utils.py` | 13 | Unclosed-candle drop, duplicate/gap/NaN rejection |
| `unit/test_order_reconciliation.py` | 12 | Client IDs, idempotency, timeout/unknown state |
| `unit/test_position_manager.py` | 9 | One-position rule, restart recovery, corrupted state |
| `unit/test_kill_switch.py` | 9 | Engage/disengage, persistence, fail-closed |
| `unit/test_orb_strategy.py` | 4 | ORB happy path + no-trade paths |
| `unit/test_security_hygiene.py` | 3 | No secrets, no dangerous constructs, no `.env` |
| `integration/test_server_endpoints.py` | 8 | API endpoints, stale-data rejection, kill switch |
| `integration/test_decision_engine.py` | 8 | Decision gates, R:R, risk sizing |
| `integration/test_exit_monitor.py` | 13 | SL/TP/invalidation/stale, reconciliation, full round trip |
| `regression/test_backtest_no_lookahead.py` | 7 | No-look-ahead (fault-injection verified) |
| `regression/test_session_dst.py` | 6 | DST/timezone correctness |
| `regression/test_lookahead_bias.py` | 2 | ORB retest/entry look-ahead (fault-injection verified) |
| `regression/test_session_open_anchor.py` | 1 | ORB session anchoring |
| `e2e/test_paper_dry_run_scenarios.py` | 9 | Full pipeline: good/weak signal, stale data, API error, limits, duplicates, restart |

---

## Fault-injection verification

Two protections were verified by breaking the code on purpose and confirming failure:

| Injected bug | Tests that caught it |
|---|---|
| `visible_market` → `market` in ORB retest/entry | 2 failed (`assert 105.0 == 100.25`) |
| `BacktestContext.visible_market` exposes full market | 3 failed, incl. *past decisions changed*: `assert [107.9,...] == [104.9,...]` |

Both fixes restored; all tests green afterwards.

---

## Determinism

Suite executed 3 consecutive times with identical results (`225 passed` each run) after runtime state was isolated to a per-session temp directory. Prior to that isolation, results depended on leftover on-disk state from earlier runs — a real fragility that was fixed, not worked around.

---

## What the green suite does *not* prove

Stated explicitly to avoid false confidence:

- **It does not prove either strategy is profitable.** Backtests show both are FRAGILE (see `AUTOTRADING_BACKTEST_REPORT.md`).
- **It does not prove the Bybit demo adapter works** — every adapter test is mocked; the real API has never been contacted.
- **It does not cover continuous operation** — there is no scheduler loop to test.
- **It does not cover multi-process deployment** — shared class-level state is untested under concurrency.
