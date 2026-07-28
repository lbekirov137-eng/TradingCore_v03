# TradingCore — Test Matrix

**Total: 225 tests — 225 passed, 0 failed, 0 skipped, 0 xfail.**
No test is disabled, skipped, or weakened to obtain a pass.

```bash
.venv/Scripts/python.exe -m pytest tests/ -v
```

| Layer | Location | Count |
|---|---|---|
| Unit | `tests/unit/` | 171 |
| Integration | `tests/integration/` | 29 |
| Regression | `tests/regression/` | 16 |
| End-to-end | `tests/e2e/` | 9 |

---

## Coverage by required category

| Required category | Where | Count | Status |
|---|---|---|---|
| Unit tests | `tests/unit/*` | 171 | ✅ |
| Property tests | `test_position_sizing.py` (Hypothesis) | 2 properties × 200/50 examples | ✅ |
| Integration tests | `tests/integration/*` | 29 | ✅ |
| No-look-ahead tests | `test_lookahead_bias.py`, `test_backtest_no_lookahead.py` | 9 | ✅ **fault-injection verified** |
| Position-sizing tests | `test_position_sizing.py` | 31 | ✅ |
| Timeout / unknown-state | `test_order_reconciliation.py` | 12 | ✅ |
| Duplicate-order tests | `test_order_reconciliation.py`, `test_paper_broker.py`, e2e `test_g` | 4 | ✅ |
| Partial-fill tests | `test_paper_broker.py` | 3 | ✅ |
| Restart recovery | `test_paper_broker.py`, `test_position_manager.py`, `test_order_reconciliation.py`, e2e `test_h`/`h2` | 7 | ✅ |
| Stale-data tests | `test_regime_filters.py`, `test_candle_utils.py`, `test_server_endpoints.py` | 6 | ✅ |
| Kill-switch tests | `test_kill_switch.py` | 9 | ✅ |
| Backtest tests | `test_backtest_engine.py` | 15 | ✅ |
| Paper-forward tests | `tests/e2e/*` | 9 | 🟡 replay only — no live 72h run |
| Bybit Demo mocked tests | `test_bybit_demo_adapter.py` | 23 | ✅ mocked only, never connected |
| One full end-to-end paper trade | `test_exit_monitor.py::TestFullRoundTrip` | 2 | ✅ open → TP/SL → realized PnL |

---

## Fault-injection verification (tests proven to actually work)

Two critical protections were verified by **deliberately breaking the code** and confirming the tests fail — not by assuming green means correct.

**1. Look-ahead in ORB retest/entry** (original audit finding F2)
```
sed -i 's/visible_market/market/' orb/retest.py orb/entry.py
→ FAILED test_retest_ignores_future_candle   assert 105.0 == 100.25
→ FAILED test_entry_ignores_future_candle    assert 105.0 == 100.25
```

**2. Look-ahead in the backtest engine**
```
BacktestContext.visible_market patched to expose the full market
→ FAILED test_strategy_never_sees_more_than_current_index
→ FAILED test_no_future_candle_is_ever_visible
→ FAILED test_appending_future_candles_does_not_change_past_decisions
      assert [107.9, ...] == [104.9, ...]   # past decisions changed
```

Both fixes were restored and all tests pass again.

---

## Key invariants under test

| Invariant | Test |
|---|---|
| Future candles cannot influence past decisions | `test_appending_future_candles_does_not_change_past_decisions` |
| Entry never fills on the decision candle | `test_entry_never_fills_on_the_decision_candle` |
| Exit never occurs on the entry candle | `test_exit_never_happens_on_the_entry_candle` |
| SL+TP in one candle → stop chosen, never the profit | `test_stop_and_tp_in_same_candle_resolves_to_stop_not_profit`, `test_both_stop_and_tp_in_same_candle_picks_conservative_stop` |
| A timeout never causes a blind order resend | `test_reconciliation_query_failure_stays_unknown_never_resends` |
| Restart replay never opens a duplicate position | `test_h_restart_replaying_same_signal_never_opens_a_second_position` |
| A genuinely new signal after restart still trades | `test_h2_restart_then_genuinely_new_signal_can_still_trade` |
| Approved position never exceeds available balance (no leverage) | `test_property_approved_result_never_exceeds_available_balance` (Hypothesis, 200 examples) |
| NaN/Inf/negative inputs always rejected | `test_property_any_bad_balance_is_always_rejected`, `TestRejectionCases` |
| Undetermined regime blocks trading | `test_undetermined_regime_blocks_trading` |
| Stale data blocks trading in live mode | `test_stale_data_is_rejected_without_replay_flag` |
| Corrupted state fails closed, never open | `test_corrupted_state_file_defaults_to_engaged_not_crash` |
| Kill switch blocks entries but survives restart | `test_state_survives_new_instance_same_path` |
| Production Bybit endpoint rejected in demo mode | `test_production_endpoint_rejected_in_demo_mode` |
| Retries reuse identical `orderLinkId` | `test_rate_limit_recovers_on_retry_with_same_order_link_id` |
| Secrets never appear in headers/logs | `test_secret_never_appears_in_headers` |

---

## Test isolation

All runtime state (paper broker, idempotency store, positions, kill switch) is redirected to a per-session temp directory by a session-scoped autouse fixture in `tests/conftest.py`. Before this was added, tests read/wrote the real `state/` directory and results depended on run history. Suite determinism is now verified across consecutive runs.

---

## Known coverage gaps (honest)

| Gap | Impact |
|---|---|
| No live Bybit demo test | Adapter behavior against the real API is **unverified**; response-shape assumptions come from docs, not observed traffic |
| No WebSocket tests | WS client does not exist |
| No multi-process concurrency tests | `PositionManager`/`DailyRiskGuard`/broker use class-level state, unsafe under `uvicorn --workers >1` |
| No 72h continuous paper-forward | Requires a scheduler loop that does not exist |
| No Monte Carlo / confidence intervals | Meaningless at ORB's n=7; VWAP already unambiguous |
| Backtest bypasses `DecisionEngine` | Daily limits, kill switch, R:R gate not exercised by backtest numbers |

---

## Before / after this phase

| | Session start | Now |
|---|---|---|
| Tests | 67 | **225** |
| Backtest engine | did not exist | deterministic, no-look-ahead, fault-injection verified |
| Paper broker | did not exist | fills, fees, slippage, partial fills, persistence |
| Exit monitor | did not exist | SL/TP/invalidation/stale + conservative same-candle rule |
| Order reconciliation | did not exist | client IDs, idempotency, timeout/unknown handling |
| Kill switch | did not exist | persistent, fails closed |
| Regime/liquidity filters | did not exist | wired into both strategies |
| Bybit demo adapter | did not exist | REST implemented, mocked-tested, never connected |
| Strategies | ORB only | ORB + VWAP — **both measured FRAGILE** |
