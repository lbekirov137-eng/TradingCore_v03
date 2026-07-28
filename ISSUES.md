# TradingCore — Open Issues

Ordered by severity. Updated 2026-07-28 (Priorities 1–8 session). Issues resolved
in earlier sessions are in `AUTOTRADING_RISK_REGISTER.md`; this file tracks what
was open at the start of this session and what changed.

---

## RESOLVED THIS SESSION

### ~~C-1 (partial) — Neither strategy has a demonstrated edge~~ → STILL OPEN, but root-caused further
See CRITICAL section below — this is not resolved, but a major contributing bug was found and fixed (see next item).

### ✅ NEW CRITICAL FINDING, FIXED — `TakeProfit.calculate` ignored trade direction
Found by tracing an anomalous "0 trades over 4 months" result on real 6-month data (not by inspection). A SHORT ORB trade opened, price moved 33% in its favor, and the position never closed because its take-profit was computed in the LONG direction regardless of actual trade direction — placing it above both entry and stop, unreachable on the downside. The stuck position silently blocked all further trading for the rest of the backtest. Fixed in `api/strategy_engine/strategies/orb/take_profit.py`; 5 regression tests in `tests/regression/test_take_profit_direction.py` reproduce the exact scenario.

### ✅ C-2 — No scheduler loop existed → RESOLVED
Built `api/scheduler/loop.py`: candle-close-aligned ticking, automatic `ExitMonitor` invocation every tick, reconciliation of pending orders, heartbeat, structured logging, Telegram-mock alerts, graceful shutdown (`stop()` + SIGINT/SIGTERM in `scripts/run_paper_loop.py`). 10 tests in `tests/integration/test_scheduler_loop.py`. 72-hour paper-forward is now **structurally possible** (was not, before).

### ✅ H-1 — Exit monitor not wired into any loop → RESOLVED
Now called automatically every scheduler tick.

### ✅ H-6 — Dormant risk guards → RESOLVED
`LossStreakGuard`, `CooldownAfterLossGuard`, `MaxDrawdownGuard`, `MaxTradesPerSessionGuard`, plus a new `DailyLossGuard` (realized loss, distinct from the existing planned-risk `DailyRiskGuard`) and `MaxOpenPositionsGuard` are now split into separately-testable classes (`api/risk/guards.py`) and wired into `DecisionEngine.decide()` — every one is checked before a trade is approved, and `TradeEngine` registers outcomes on open/close. 24 dedicated unit tests + 5 integration tests proving each guard actually blocks a trade with a machine-readable reason.

One real bug was found and fixed while wiring `MaxDrawdownGuard`: it initially measured "equity" as cash balance alone, so simply *opening* a position (converting cash to an asset of equal value) looked like an instant ~13% drawdown. Fixed to measure cost-basis equity (cash + held position at entry cost) and to query the broker directly rather than `PositionManager` (which a simulated restart correctly wipes, but the broker's position survives — exactly the scenario that needs catching).

### ✅ Performance — `BacktestEngine` was effectively O(n²)
Recomputing EMA/RSI/ATR/structure from the *entire* visible history on every candle meant total backtest cost grew quadratically with data length. Fixed by bounding indicator inputs to a configurable recent window (`indicator_lookback`, default 260 candles — comfortably enough for EMA200/RSI14/ATR14 to be numerically identical to full-history computation). Does **not** affect `visible_market` (still full, exact, no-look-ahead-affecting history) or look-ahead guarantees. 3 regression tests confirm identical results when data is shorter than the window.

---

## CRITICAL (still open)

### C-1 — Neither strategy has a demonstrated, adequately-sampled edge
- ORB: 7 trades over ~6 weeks — statistically meaningless (`min_sample_size_check` now correctly flags this).
- VWAP: 98 trades, profit factor 0.092, 0% walk-forward consistency, −12.8% drawdown — adequate sample, decisively unprofitable.
- This blocks Gate B and everything downstream. See `AUTOTRADING_NEXT_ACTIONS.md` for concrete next steps (higher timeframe, fix the degenerate retest, or reject the formulations).

---

## HIGH

### H-2 — Backtest and paper broker are still separate execution paths
Unchanged from before: the backtest simulates fills internally rather than routing through `PaperBroker`, so it doesn't exercise `DecisionEngine`'s gates (all the guards above, R:R check, duplicate guard). A true multi-day replay through the live execution path has not been built.

### H-3 — Bybit demo adapter has never contacted the real API
Unchanged: all 23 tests mocked, no credentials configured (by design), schema assumptions from docs.

### H-4 — No WebSocket client
Unchanged: REST polling only, with retry/backoff and rate-limit handling added this session (`api/market_data/resilience.py`), but no WS transport exists.

### H-5 — Class-level mutable state is not multi-process safe
Unchanged, and now applies to more state: `PositionManager`, `DailyRiskGuard`, and all five new guards in `api/risk/guards.py` use class-level state. Running `uvicorn --workers 2` would give each worker an independent view of every limit.

### H-7 — NEW: VWAP's own session-VWAP calculation is still O(n × session_length)
The `BacktestEngine` indicator-lookback fix above does **not** touch this — `calculate_session_vwap` is called directly by the VWAP strategy (not through the engine's bounded EMA/RSI/ATR path) and still recomputes cumulative sums from session start on every call. Confirmed still slow on the 6-month dataset. Needs an incremental/cached implementation (see `AUTOTRADING_NEXT_ACTIONS.md`).

---

## MEDIUM

### M-1 (updated) — VWAP performance
See H-7 above — this was previously rated MEDIUM but is now blocking practical multi-month VWAP research; consider it borderline HIGH.

### M-4 — ORB retest is degenerate (unchanged)
`Breakout` and `Retest` still key off the same final candle. Not touched this session (would require product/quant design work on the intended multi-bar window, out of scope for "small, safe fixes").

### M-7 — Duplicate/dead architecture still present (unchanged)
`api/pipeline_v2/`, `api/core/` (unwired), `api/market_data.py` (shadowed), `api/main.py` (unused second app), `ExchangeRouter` (zero callers).

---

## LOW (unchanged from prior session)

- `python-dotenv` declared but never imported.
- Single symbol tested at meaningful scale so far (BTCUSDT); 6-month data now fetched but full-scale multi-regime backtest is compute-heavy in this environment (see `AUTOTRADING_BACKTEST_REPORT.md` for what completed vs. what's still running/pending).
- Funding costs not modeled (irrelevant for spot).
- `starlette` TestClient deprecation warning (cosmetic).

---

## New research/tooling added this session (not bugs — capability additions)

- `api/backtesting/research.py`: minimum sample-size check, buy-and-hold and NO_TRADE benchmarks, regime segmentation, parameter-stability analysis, Monte Carlo trade-order permutation.
- `api/strategy_engine/strategies/orb/candidates.py`, `.../vwap/candidates.py`: labeled, independently-evaluated candidate variants (range/ATR filter, min relative volume, tighter/wider pullback, volume confirmation) — none merged into the baseline, none combined with each other.
- `BacktestConfig.time_stop_candles`: optional forced-close-after-N-candles, for candidate research only (off by default).
