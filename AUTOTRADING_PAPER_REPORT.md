# TradingCore — Paper Trading Report

**Date:** 2026-07-28

> **No paper-forward run has been performed.** This report documents what the paper trading engine *does*, what has been *verified*, and precisely why a continuous forward run has not happened yet. It contains no simulated results presented as real ones.

---

## 1. Status

| Item | Status |
|---|---|
| Paper broker implemented | ✅ `api/paper_broker/paper_broker.py` |
| Deterministic replay verified | ✅ identical inputs → identical final state |
| Full open→exit round trip verified | ✅ `tests/integration/test_exit_monitor.py::TestFullRoundTrip` |
| Restart persistence verified | ✅ broker, positions, orders, kill switch |
| **Continuous 72h paper-forward run** | ❌ **NOT PERFORMED** |
| **Multi-day historical replay through the paper broker** | ❌ **NOT PERFORMED** |

---

## 2. Why no forward run happened

Two hard blockers, both documented in `ISSUES.md`:

1. **There is no scheduler loop (C-2).** `Scheduler.tick()` runs once per HTTP request to `/paper/tick`. Nothing calls it on a schedule, and nothing calls `ExitMonitor.check()` automatically. A position opened in paper mode today would remain open indefinitely, because no code path monitors it. Running a "72-hour paper forward" would mean manually curl-ing an endpoint for three days and no exits would ever trigger.

2. **No strategy has a demonstrated edge (C-1).** Gate B fails. ORB nets +0.37 USDT against 2.98 USDT of fees over 7 trades; VWAP loses 128 USDT over 98 trades with 0% walk-forward consistency. A forward run would generate noise, not evidence.

Starting a forward run before fixing these would produce a report that *looks* like validation while proving nothing — precisely the false confidence this project's audit exists to prevent.

---

## 3. What the paper engine actually does

`api/paper_broker/paper_broker.py` implements the same `ExchangeAdapter` interface as the Bybit demo adapter, so the decision engine, reconciler, and exit monitor operate identically against either.

| Capability | Implementation | Verified by |
|---|---|---|
| Market orders | Immediate fill at reference price + slippage **against** the trader | `test_market_buy_fills_immediately_with_slippage_and_fee` |
| Limit orders | Rest until price range touches them | `test_limit_order_stays_open_until_price_touched` |
| Partial fills | `liquidity_per_check` caps fill per candle; remainder stays open | `test_partial_fill_when_liquidity_limited` |
| Fees | Charged on entry and exit, deducted from balance | `test_fees_are_charged_and_reduce_pnl` |
| Slippage | 5 bps/side, always adverse | `test_slippage_worsens_entry_price` |
| Balance / no leverage | Buy rejected if notional+fee exceeds balance; never goes negative | `test_buy_rejected_when_insufficient_balance_never_goes_negative` |
| Realized PnL | Computed against weighted-average entry on close | `test_market_sell_realizes_pnl` |
| Audit ledger | Append-only event log (placed/filled/partial/cancelled/rejected/position open/close) | inspected in tests |
| Restart persistence | Atomic JSON write; new instance restores balance, positions, orders, ledger | `test_state_survives_new_instance_same_path` |
| Corrupted state | Falls back to fresh state, never crashes | `test_corrupted_state_file_falls_back_to_fresh_state_not_crash` |
| Idempotency | Same `client_order_id` never double-fills | `test_placing_same_client_order_id_twice_does_not_double_fill` |

### Exit handling (`api/execution/exit_monitor.py`)

| Trigger | Behavior |
|---|---|
| Stop loss | Close at stop |
| Take profit | Close at TP1 |
| **SL and TP in the same candle** | **Closes at STOP** — the profitable outcome is never auto-selected, because intrabar ordering is unknowable from OHLC |
| Invalidation | Close at current price |
| Stale position | Close after max age (default 24h) |
| Broker reports flat | Local state reconciled, no phantom position retained |
| Reconciliation fails | Position **held**, never blindly closed |

---

## 4. Verified end-to-end scenarios

All 9 scenarios in `tests/e2e/test_paper_dry_run_scenarios.py` pass deterministically:

| Scenario | Result |
|---|---|
| Quality signal | `TRADE` → `OPENED` |
| Weak signal | `NO_TRADE`, no position |
| Stale / missing data | Safe `NO_TRADE`, no crash |
| Exchange API error | Safe `NO_TRADE`, no crash, no HTTP 500 |
| Insufficient data for ATR | `NO_TRADE` |
| Daily trade limit exceeded | `NO_TRADE` with reason |
| Duplicate order | Blocked, no second position |
| **Restart replaying same signal** | `ORDER_PENDING`, **no duplicate position** |
| **Restart then genuinely new signal** | `OPENED` normally |

The restart pair is worth highlighting: an earlier version of the test asserted that replaying a signal after restart should open a *new* position. That encoded unsafe behavior — in production the first order was already filled, so replaying would silently double the position. The test was corrected to assert the safe outcome.

---

## 5. Full round trip (verified)

`tests/integration/test_exit_monitor.py::TestFullRoundTrip`:

- Open at 100.0, stop 98.0, TP 104.0, qty 1.0 → position opened via broker
- Candle with high 104.5 → TP triggered → position closed → **realized PnL positive**
- Separate case: candle with low 97.5 → stop triggered → **realized PnL negative**

Both confirm money actually moves through the broker's balance and realized-PnL accounting, not just state flags.

---

## 6. What a real paper-forward run will require

1. A scheduler loop calling `Scheduler.tick()` → `ExitMonitor.check()` → `reconcile_all_pending()` on each closed candle.
2. A strategy that survives its own cost base (Gate B).
3. Routing the backtest through `PaperBroker` so replay and live share one execution path.
4. Wiring `LossStreakGuard` into the decision path so cooldown/drawdown limits are live.
5. Multi-process-safe state if more than one worker is ever used.

Until item 1 exists, "paper forward" is not a thing this system can do.
