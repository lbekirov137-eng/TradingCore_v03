# TradingCore — Independent Full Audit Report

**Date:** 2026-07-28
**Scope:** Full repository, paper/demo mode only. No live orders, no mainnet, no leverage, no real money at any point in this audit.
**Posture:** Adversarial/independent review — existing tests and existing code were not trusted by default; critical formulas were independently recalculated; empirical checks were run against live (read-only) exchange endpoints where relevant.

---

## 0. Executive summary

The repository is **pre-production, actively under construction**, with two partially-overlapping architectures (`api/pipeline` v1 vs `api/pipeline_v2`/`api/core` v2 — the v2 stack is dead/unwired scaffolding). Before this audit, the "new" ORB/Decision/Scheduler/Workflow vertical (the closest thing to a real paper-trading loop) **did not run at all** — it failed at import time (`ORBStrategy` class didn't exist) and would have failed again at call time (`DecisionEngine.decide` didn't exist, indicators were never computed before the strategy needed them, and there was zero exception handling around network/data calls).

Beyond those blocking defects, independent recalculation and empirical testing against live Binance/Bybit endpoints turned up **9 CRITICAL and 8 HIGH severity issues**, several of which are exactly the class of bug this audit was commissioned to find: a confirmed **look-ahead bias** bug, a confirmed **unclosed-candle / repainting** bug (verified live against both exchanges), a **risk engine that silently accepted NaN/Infinity** as a valid position size, a **position-lock that was hard-coded to always return "no open position"** (i.e. a complete no-op), and an **ORB "Opening Range" that was not anchored to any real session boundary** for the default (crypto) symbol.

All of the above were fixed with small, targeted, reversible changes, each backed by a regression test that is proven (not assumed) to fail without the fix — verified by reverting two of the fixes live and watching the tests fail, then restoring them. The full test suite (67 tests: unit, regression, integration, e2e) passes after the fixes. **No profitability claim is made or possible**: there is no working backtest engine in this repository (`api/backtesting/backtest_engine.py` is an empty stub), so sections on walk-forward validation, out-of-sample testing, and Monte Carlo robustness (audit sections 10–11) are **BLOCKED**, not completed — see §10.

**Nothing in this repository can place a real order.** `api/binance.py` and `api/bybit.py` only implement read-only `GET` endpoints (klines, ticker, orderbook). This is independently confirmed and is the strongest safety property of the current system.

---

## 1. Structure, entry points, and actual data flow

Entry points found:
- `main.py` (root) — CLI, calls `MarketAnalyzer.analyze()` once, prints JSON. Not wired to any of the safety fixes below except transitively through `MarketAnalyzer`.
- `api/server.py` — **the real container entry point** (`docker-compose.yml` runs `uvicorn api.server:app`). Before this audit: `/` and `/analyze` only. Added in this audit: `GET /paper/tick` (the actual reachable "paper/demo" tick, see §1.1) and `GET /safety` (non-secret startup safety summary).
- `api/main.py` — a **second, disconnected** FastAPI app (`/health`, `/ping`, `/market`) that is not referenced by `docker-compose.yml` and is not the same `app` object as `api/server.py`. Confirmed dead/unused as a deployable entrypoint; flagged as confusing duplication (§8, MEDIUM).
- No Telegram integration exists (only mentioned in a docstring in `api/workflow/workflow.py`). No background scheduler/cron loop exists — `Scheduler.tick` is a single synchronous call, triggered per-request, not a recurring loop.

### 1.1 Actual flow (as built, after fixes)

```
GET /paper/tick
  -> LiveContext(exchange, symbol, interval, limit)
  -> Workflow.run(context)
       -> Scheduler.tick(context)          [try/except safety boundary — see F8]
            -> DataEngine.load(...)         -> MarketHub.get_klines(...)
                 -> provider.get_klines(...) [Binance/Bybit REST, read-only]
                 -> drop_unclosed_candle(...)  [F1 — new]
                 -> validate_candles(...)      [F10 — new]
            -> compute indicators (EMA/RSI/ATR/structure)
            -> StrategyEngine.generate(context) -> ORBStrategy.generate(context) [F7 — implemented]
            -> DecisionEngine.decide(context)   [F6/F8/F11/F12 — implemented]
       -> TradeEngine.execute(decision)     [F11/F13 — implemented; paper-only, journals OPENED/FAILED_SAFELY]
  -> { decision, execution }
```

Before this audit, this pipeline could not execute past import time. It is now reachable, deterministic under test, and defaults to `NO_TRADE` / `FAILED_SAFELY` on every failure path exercised in §9.

A second, older pipeline (`MarketAnalyzer.analyze()` → `api/pipeline/pipeline.py` → `DecisionEngine.process()`, always hard-coded `NO_TRADE`) still backs `/analyze`. It was **not** rewired to the new decision logic — it is intentionally left as the safe, inert legacy stub it already was, but it is now wrapped in a try/except so exchange/network failures return a safe JSON body instead of an HTTP 500 (F8b).

No obvious way exists in the current code to open a trade **without** going through `DecisionEngine.decide` → `TradeEngine.execute` (§15 cross-module invariants, checked in tests).

---

## 2. Market data audit

| # | Finding | Evidence | Severity |
|---|---|---|---|
| F1 | **Both Binance and Bybit return the still-forming (unclosed) candle as the last kline.** Verified empirically live (see below) — not theoretical. | `api/binance.py`, `api/bybit.py`, `api/market_data/market_hub.py` | **CRITICAL** |
| F10 | **Zero data validation existed anywhere.** No check for duplicate timestamps, out-of-order candles, gaps, negative/zero/NaN prices, or `low > high`. Raw exchange JSON was passed straight to indicators/strategy. | `api/data_engine.py`, `api/market_data/market_hub.py` (before fix) | **HIGH** |
| F14 | No `stale-data` age check exists beyond "is the last candle closed" — e.g., if an exchange stops updating but still returns a syntactically valid, well-formed old candle series, nothing currently flags it as stale relative to wall-clock time beyond the closed-candle filter. | — | MEDIUM (not fixed — needs a "max age since last candle close" check; recommended next step) |
| F19 | Three independent, uncoordinated notions of "is a trading session open" exist: `SessionResolver` (NY 9:30-16:00 local / London 8-16 local / CRYPTO fallback), `config/trading_sessions.py` (a completely separate, unused UTC schedule dict), and `SessionRule` in the decision-engine rules (hard-coded UTC 7-16 window, unrelated to either of the above). | `config/session_resolver.py`, `config/trading_sessions.py`, `api/decision_engine/rules/session_rule.py` | MEDIUM |
| F20 | `ExchangeRouter` (auto exchange selection + health check) is **dead code** — never called by `DataEngine`/`MarketHub`/`Scheduler`. `AUTO_SELECT_EXCHANGE`/`DEFAULT_DATA_EXCHANGE` config values have zero runtime effect; exchange is simply whatever literal string the caller passes. | `api/exchange_router.py` — confirmed via repo-wide grep, zero callers | MEDIUM |
| F22 | `api/market_data.py` (flat module, `class MarketData`, Bybit-only) is **permanently shadowed** by the `api/market_data/` package — confirmed empirically (`import api.market_data` resolves to the package `__init__.py`, never the flat file). Unreachable dead code. | — | LOW |

**Empirical proof of F1** (run live against both exchanges before the fix, from this session):

```
Binance 5m klines, limit=5 — last candle close_time > now:
  open 1785213300000  close_time 1785213599999  now 1785213470960  closed? False

Bybit 5m klines, limit=5 — last candle (after provider's reverse()) same shape:
  open 1785213300000  est_close 1785213600000  now 1785213480149  closed? False
```

Using this candle's `close`/`high`/`low` as final values means every indicator and the ORB breakout/retest check was evaluating a price that could still move before the candle actually closes — a live-repainting defect that directly contradicts `ORB_BASELINE.md` rule #2 ("Вход только после закрытия свечи за диапазоном").

**Fix:** `api/market_data/candle_utils.py` (new) — `drop_unclosed_candle()` computes each candle's close time from `open_time + interval_ms` and drops the last candle if its close time is still in the future; `validate_candles()` rejects duplicate/out-of-order/gapped timestamps and non-positive/NaN OHLCV values. Both are wired into the single choke point `MarketHub.get_klines`, so **every** consumer (old and new pipeline alike) is covered. Re-verified end-to-end against **live** Binance and Bybit data after the fix (49/49 candles kept after dropping the unclosed one from a 50-candle request, on both exchanges).

**Tests:** `tests/unit/test_candle_utils.py` (21 cases — drop-unclosed, duplicate, out-of-order, gap, non-positive/NaN price, negative volume, `low>high`, close-outside-range, empty series).

---

## 3. Look-ahead bias / data leakage audit

### F2 — CRITICAL, CONFIRMED: `Retest` and `Entry` read `context.market` instead of `context.visible_market`

- **File/function:** `api/strategy_engine/strategies/orb/retest.py:Retest.detect`, `api/strategy_engine/strategies/orb/entry.py:Entry.calculate`
- **Repro:** `OpeningRange.calculate` and `Breakout.detect` correctly read `context.visible_market` (the backtest-time-truncated view). `Retest`/`Entry` read `context.market` (the **full**, untruncated series) for their own price lookups. In live mode the two happen to be identical (see `LiveContext.visible_market` property added in this audit), but in `BacktestContext` they diverge by design — `visible_market` is truncated to `[:index+1]`, `market` is the whole dataset.
- **Expected:** entry/retest price must come from the last candle *visible* at decision time.
- **Actual (before fix):** entry/retest price came from the **last candle in the entire dataset**, including candles that occur after the decision point — classic look-ahead bias, invisible in live mode but would silently invalidate any backtest run against this code once `BacktestEngine` is built.
- **Fix:** both now read `context.visible_market`.
- **Proof of test validity:** the regression test was run against the **reverted** code and confirmed to fail (`assert 105.0 == 100.25` — the future candle's price leaked through), then the fix was restored and the test passes again. This was done live in this session, not assumed.
- **Test:** `tests/regression/test_lookahead_bias.py::test_retest_ignores_future_candle`, `::test_entry_ignores_future_candle`.

### F3 — CRITICAL, CONFIRMED: ORB "Opening Range" for CRYPTO was not anchored to any real session boundary

- **File/function:** `api/strategy_engine/strategies/orb/session_open.py:SessionOpen.find_first_candle`
- **Repro:** for `session.name == "CRYPTO"`, the function **unconditionally returned index 0** regardless of the actual timestamp. `DataEngine.load` fetches a **sliding window** of "the most recent N candles" on every call — so "index 0" is a different point in time on every single tick (whatever candle happens to be oldest in that call's window), not a fixed session start. Since `BTCUSDT`/crypto is the default and — given `SessionResolver`'s narrow NY/London windows — the dominant path, this means the "Opening Range" that the entire ORB strategy is built on **was not the first 5 minutes of any real session**, contradicting `ORB_BASELINE.md` rule #1 verbatim.
- **Fix:** for CRYPTO, the opening range now anchors to the most recent UTC midnight (`config/trading_sessions.py` already defines CRYPTO as `00:00–23:59` — the code just didn't use it) and finds the first candle at or after that boundary.
- **Residual risk:** this fix **could not be validated by backtest** because no backtest engine exists (§10). It is a correctness fix that makes the code match its own specification, not a performance optimization — but it has not been proven to produce better or even different live outcomes, since ORB has never actually traded live before this audit.
- **Test:** `tests/regression/test_session_open_anchor.py` (candles start 3 bars before UTC midnight; asserts the range is built from the post-midnight candles, not the array's first 3).

### F4 — HIGH: Retest and Breakout key off the same single candle

`Breakout.detect` and `Retest.detect` both inspect `closes[-1]` of the same final candle. This means "retest" is not actually a separate, later multi-bar pullback event — it collapses into "did the breakout candle itself close within 15% of the level." No genuine multi-candle retest sequence over time is verified anywhere in the code. **Not fixed** — this needs a product/quant decision on the intended retest window (e.g., N candles after breakout) and is out of scope for a safe, small change; documented here as a design gap, not silently left undiscovered.

### No other look-ahead vectors found

Checked and **not** found in this codebase (because the relevant features don't exist yet, not because they were verified clean): VWAP calculation (§4 below — not implemented), market regime scoring using future data (not implemented), daily high/low computed mid-day (`MarketStructure.analyze` only compares the last two candles — no full-day high/low logic exists at all).

---

## 4. ORB strategy audit

Per `ORB_BASELINE.md`:

| Rule | Status |
|---|---|
| Opening Range = first 5 minutes of session | **Was broken for CRYPTO (F3), fixed this session.** NY/London path uses local-time comparison — not independently stress-tested against DST transitions in this audit; flagged as a residual risk. |
| Entry only after candle close beyond range | Partially true after F1 fix (unclosed candle now dropped upstream) — but see F4 (retest doesn't verify a genuinely later close). |
| Mandatory retest | Present in code but degenerate per F4. |
| Confirmation | Present (`Confirmation.check`), trivial (just ANDs breakout+retest booleans) — no additional confirmation signal (volume, structure, EMA) is actually used despite `CancelScenario` listing them as TODOs. |
| Stop behind range | Present (`StopLoss.calculate`), correctly uses ATR-adjusted range edge. |
| TP 1:2 / 1:3 | Present, and now the *decision layer* independently recomputes and enforces R:R ≥ `MIN_RISK_REWARD` (2.0) rather than trusting the label string `"1:2 / 1:3"` at face value (F6). |
| Only one open position | **Was a complete no-op (F5), fixed this session.** |
| No averaging / no re-entry | No averaging code exists anywhere (positive). Re-entry into the *same session* is now blocked (F11 — new); re-entry into a *different* session after a close is allowed by design. |
| Min/max opening-range length, volatility/liquidity filters | **Not implemented at all** — `config/adaptive_orb.py` (`MIN_ORB_MINUTES`, `MAX_ORB_MINUTES`, `ATR_LOW/HIGH`, `VOLUME_LOW/HIGH`, `MIN_LIQUIDITY_SCORE`, `MIN_CONFIDENCE`) is **100% dead config** — confirmed via repo-wide grep: referenced nowhere outside its own file. `CancelScenario.check` literally lists Funding/Liquidity/Volatility/Wyckoff/News filters as `# TODO`-style comments with no code. |

**Net assessment:** the ORB implementation is a real, testable skeleton of the documented rules, but it currently trades identically in trending, ranging, illiquid, high-volatility, and news-shock conditions — there is no regime/liquidity/volatility differentiation anywhere in the decision path, despite this being an explicit, named requirement in the project's own backlog and config scaffolding.

---

## 5. VWAP Trend Pullback audit

**Not applicable — this strategy does not exist in the codebase.** No file, class, or function referencing VWAP was found anywhere (`grep -ri vwap` across the repo: zero matches outside this report). This cannot be audited because it was never built; it is a backlog gap, not a bug.

---

## 6. Market regime, liquidity, and strategy scoring

**Confirmed: none of these participate in the final decision.** `context.regime` exists as a field in `MarketContext` (contracts/context.py) but is **never written to by any code** — confirmed via repo-wide grep (`context.regime` only appears in its own dataclass definition). There is no code anywhere that distinguishes trend vs. range, high vs. low volatility, illiquid markets, news shocks, abnormal spread, or unstable feed. An "undetermined regime" cannot force `NO_TRADE` today because no regime is ever determined in the first place — the decision path only checks: strategy-signal approval → position/duplicate gates → R:R → risk limits → daily limits. This is a real, named gap relative to the project's own stated priorities (Priority C in the original brief) and is **not fixed** in this pass (it is a feature build, not a bug fix, and would require product/quant design decisions about thresholds).

---

## 7. Independent risk engine audit

All formulas below were **independently recalculated in test code**, not just compared against the engine's own output (see `tests/unit/test_risk_engine.py::TestRiskEngineIndependentRecalculation`).

**Formula (confirmed correct in principle):** `risk_amount = balance × (risk_percent / 100)`; `position_size = risk_amount / stop_distance`.

- 1000 × 0.1% = **1.0** (risk_amount) — independently recomputed and matches. ✅
- 10,000 × 0.1% = **10.0** — independently recomputed and matches. ✅

### F6 — CRITICAL, CONFIRMED: risk was sized from raw ATR, not the real stop distance

`RiskEngine.calculate(atr=...)` treats its `atr` parameter as the stop distance. For the *old* pipeline (`TradePlan.build`), the stop **is** defined as `price ± atr`, so this is self-consistent. For **ORB**, the stop is `opening_range_low − 0.2×ATR` (or symmetric for shorts) — a **different number from raw ATR**. Any code path that called `RiskEngine.calculate(atr=raw_atr)` for an ORB signal would size the position against the wrong distance, silently breaking the "~0.1% risk per trade" target by an amount that depends on how far the entry is from the opening-range edge relative to ATR — this can be a large, unpredictable divergence, not a rounding error.

**Fix:** `DecisionEngine.decide` now computes `risk_distance = abs(entry − stop)` from the actual trade plan and passes **that** into `RiskEngine.calculate`, not the raw ATR. **Test:** `tests/integration/test_decision_engine.py::test_risk_is_computed_from_real_stop_distance_not_raw_atr` (constructs entry=100, stop=98 and asserts `risk["stop_distance"] == 2.0`, independently derived from the trade plan, not from any ATR value).

### F9 — CRITICAL, CONFIRMED: NaN/Infinity/negative/zero silently passed the `allowed` check

- **Root cause:** `if atr <= 0` was the only guard. In Python/IEEE-754, `float('nan') <= 0` is `False` and `float('inf') <= 0` is `False` — so **both silently passed** as "not zero-or-negative," producing `"allowed": True` with a `NaN` or `0.0` position size. Independently verified in this session:
  ```python
  >>> float('nan') <= 0
  False
  >>> risk_amount / float('nan')
  nan   # returned with allowed=True, before fix
  ```
- **When this triggers in practice:** `ATREngine.calculate` uses a 14-period rolling mean; with fewer than 14 candles (startup, a data gap, a symbol with a short history) ATR is `NaN`. Before this fix, that `NaN` would flow straight through to an "approved" trade with a `NaN` position size.
- **Fix:** `RiskEngine.calculate` now explicitly type-checks and rejects `NaN`, `±Infinity`, `None`/wrong-type, and non-positive values for **all four** inputs (`balance`, `risk_percent`, `price`, `atr`), plus rejects `balance ≤ 0`, `risk_percent ≤ 0`, `price ≤ 0` (none of which were checked at all before).
- **Test:** `tests/unit/test_risk_engine.py::TestRiskEngineBoundaryCases` (10 cases: NaN/inf/zero/negative for each input independently).
- **Also fixed at the strategy layer:** `ORBStrategy.generate` now explicitly rejects `NaN`/non-positive ATR before even building a trade plan (`tests/unit/test_orb_strategy.py::test_nan_atr_blocks_trade_even_with_valid_breakout`).

### Other risk-engine boundary cases checked

| Case | Result before fix | Result after fix |
|---|---|---|
| `stop_distance = 0` (atr=0) | Rejected (pre-existing check) | Rejected |
| `balance = 0` | **Silently allowed**, `risk_amount=0` | Rejected |
| `balance < 0` | **Silently allowed**, negative `risk_amount` | Rejected |
| `risk_percent < 0` | **Silently allowed** | Rejected |
| `price ≤ 0` | **Silently allowed** (unused in the formula, but nonsensical) | Rejected |
| `atr = NaN` / `Infinity` | **Silently allowed** | Rejected |

### Not implemented (documented, not fixed — larger feature work)

- Tick size / lot size / quantity step / minimum notional rounding: **does not exist anywhere in the codebase.** Position sizes are raw floats with no exchange-specific precision handling.
- Fees, slippage, funding, spread: **not modeled anywhere.**
- Contract type (linear/inverse), base/quote distinction: not modeled — acceptable for now since no leverage/derivatives trading exists, but should be addressed before any exchange beyond spot-like BTCUSDT is considered.
- Maximum concurrent exposure across multiple symbols: not applicable yet since only one position total is allowed system-wide (F5), which is itself a (conservative) proxy for max exposure.
- Averaging down / martingale / leverage escalation / moving stop away after entry: **no such code exists anywhere** — confirmed via repo-wide search. This is a clean, positive finding.

---

## 8. Execution / paper simulator audit

**Confirmed: there is no live-order code anywhere in this repository.** `api/binance.py`/`api/bybit.py` only implement `get_klines`, `get_ticker`, `get_orderbook` — all `GET`, all read-only, no signing, no API keys used, no order-placement endpoint referenced anywhere. This means `LIVE_TRADING=True` in `config/settings.py` would currently have **no effect** even if flipped, because nothing reads that flag *and* nothing exists to place a real order regardless. This is the single strongest safety property of the system and was independently verified by exhaustive grep for order-placement/signing patterns.

### F5 — CRITICAL, CONFIRMED: `PositionManager.has_open_position()` was hard-coded to return `False`

- **File:** `api/position_manager/position_manager.py`
- **Repro:** the entire class was two static methods, one returning the literal `False`, the other the literal `None` — no state, no parameters used.
- **Impact:** TRADE_LIFECYCLE.md rule #1 ("одновременно только одна позиция") was **completely unenforced**. Nothing in the codebase called this method's real logic because there was no real logic to call.
- **Fix:** rewritten with real class-level state: `open_position`/`close_position`/`has_open_position`/`is_duplicate_signature`/`is_duplicate_session`/`reset`. Note this is **in-memory, single-process state** — a real process restart loses the "open position" record (see F17 below, restart/resume).
- **Test:** `tests/unit/test_position_manager.py` (7 cases, including "cannot open a second position while one is open" raising `RuntimeError`).

### F11 — HIGH, CONFIRMED: no idempotency / duplicate-order blocking existed anywhere

Before this audit, `TradeEngine.simulate(signal)` did not call `PositionManager` at all, and nothing else did either — confirmed via repo-wide grep (zero callers of `PositionManager` before this session's changes). **Fix:** `TradeEngine.execute(decision)` (new method; old `simulate` left untouched since nothing calls it) now checks, in order, before opening any position: (1) is there already an open position, (2) is this an exact-signature repeat, (3) has this session already been traded. Any of the three returns `FAILED_SAFELY` and is journaled — no phantom position is ever created. **Test:** `tests/e2e/test_paper_dry_run_scenarios.py::test_g_duplicate_order_is_blocked_safely`.

### F12 — HIGH, CONFIRMED: no daily trade-count or daily risk-budget limit existed

**Fix:** new `DailyRiskGuard` (in `api/risk_engine.py`) tracks trades-opened-today and cumulative-risk-committed-today (UTC-day-keyed, in-memory), checked by `DecisionEngine.decide` **before** any order is created (per the brief's explicit requirement), and updated by `TradeEngine.execute` only **after** a trade is actually opened (so a decision that never executes doesn't consume the quota). **Important limitation, stated honestly:** "risk committed today" is a proxy on *planned* risk at trade-open time, **not realized P&L** — there is no position-close/P&L-tracking loop yet (see F17), so a true "max daily *loss*" cannot be computed yet, only "max daily risk *committed*." **Test:** `tests/unit/test_risk_engine.py::TestDailyRiskGuard`, `tests/e2e/test_paper_dry_run_scenarios.py::test_f_daily_trade_limit_blocks_further_trades`.

### F17 — HIGH, NOT FIXED (documented): no position-exit / lifecycle-monitoring loop exists

Once `TradeEngine.execute` opens a paper position, **nothing automatically closes it.** There is no loop that checks the live price against the stored stop-loss/take-profit and triggers a close; `TradeEngine.close()` exists (new, this session) but must be called explicitly. Consequently: TP1/TP2/SL-hit detection, "SL and TP hit in the same candle" tie-breaking (the brief explicitly warns against picking the profitable outcome automatically — there is currently no code that resolves this at all, so the question is moot but unresolved), gap-through-stop handling, and realized P&L/journal-of-closes are all **unimplemented**. This is the largest remaining gap between "the system can safely decide not to trade" (true today) and "the system can run an actual paper trading session end-to-end including exits" (not true yet).

### Bug found and fixed in this session's own new code

While writing the e2e test suite, a real bug was caught in code written during this same audit: `TradeEngine.execute`/`close` built result dicts as `{"status": "OPENED", **position}` — since `position` itself carries an internal `"status": "OPEN"` key, Python dict-unpacking order meant the **inner** key silently overwrote the outer one, so `execute()` actually returned `status: "OPEN"` instead of `"OPENED"`. Caught by `tests/e2e/test_paper_dry_run_scenarios.py::test_a_good_signal_opens_paper_trade` failing immediately, fixed by reordering the dict merge (`{**position, "status": "OPENED"}`). Left in this report as a demonstration that the test suite is doing real work, not rubber-stamping.

### Not implemented (documented, not fixed)

Market vs. limit order modeling, bid/ask spread, partial fills, rejected/delayed fills, retry-after-timeout semantics, price/quantity precision — none of this exists; the paper simulator is currently a same-tick fill-at-close-price model with no realism layer beyond "did the signal pass risk/safety gates."

---

## 9. PnL and journal audit

`TradeJournal` (pre-existing, `api/backtesting/trade_journal.py`) records a flat list of trade dicts and computes `win_rate` from a `"result"` field that **nothing currently sets** — there is no code anywhere that determines whether a closed paper trade was a win or a loss (this depends on the missing exit-monitoring loop, F17). `TradeEngine.execute`/`close` now journal every `OPENED`/`FAILED_SAFELY`/`CLOSED` event (fixed this session), including all safety-gate rejections with their reason — satisfying "all decisions are journaled with a reason" for the paths that exist today. **Not yet possible to verify:** gross/net P&L, R-multiple, equity curve, drawdown, expectancy, consecutive-loss streaks — all of these require the missing exit-monitoring loop (F17) and are correctly reported here as **not computable**, not "computed and equal to zero."

**Positive, verified invariants (see `tests/e2e` and `tests/integration`):**
- A trade is never opened twice from the same signature or the same session (F11).
- Restart (simulated via `PositionManager.reset()`) safely clears state and allows a fresh, correct decision afterward — no duplicated or phantom position (`test_h_safe_restart_resume_clears_state_and_allows_new_trade`).
- Every `NO_TRADE` decision carries a reason (checked in every integration/e2e test).

---

## 10. Backtest validation — BLOCKED

**`api/backtesting/backtest_engine.py` contains no code** — it is a bare docstring-shaped stub with no `BacktestEngine` class, no loop, no statistics generation. `AdaptiveOptimizer.optimize()` (also pre-existing) returns a hard-coded `{"orb_minutes": 5, "status": "baseline"}` regardless of input. `TradeJournal.statistics()` computes win-rate from data that is never populated (§9).

**Consequence: sections 10 and 11 of the audit brief (backtest validation, statistical robustness — train/validation/out-of-sample split, walk-forward, regime coverage, fee/slippage/latency stress, parameter perturbation, Monte Carlo, confidence intervals, top-N-trade sensitivity) cannot be performed, because there is no historical simulation loop to run them against.** This is stated plainly rather than worked around with a hand-rolled substitute, per the explicit instruction not to fabricate results. **No claim that ORB (or any strategy in this repo) is profitable, marginal, or unprofitable can be made — there is not yet a mechanism to measure it.**

---

## 11. Security review

- **No secrets, API keys, or credentials found anywhere in the repository or its git history** (checked tracked files via `git grep`, and `git log -p -- '*.env*'` across all branches — zero results).
- **No `.env` file exists** in the working tree.
- `python-dotenv` is declared in `requirements.txt` but **never imported anywhere** — dead dependency; also means there is currently no mechanism that would even load a `.env` file if one were added.
- No `eval`/`exec`, no `pickle`, no `subprocess`/`os.system`/shell=True, no obvious command-injection or path-traversal surface anywhere in `api/` or `config/`.
- `except Exception: continue` (broad, silent) appears in `api/exchange_router.py` and both provider `health_check()` methods — acceptable for a boolean health probe, but it means real outages (network, auth, malformed response) are indistinguishable from "exchange not supported" in any logs, since nothing is logged at all in these paths.
- These checks are now **operationalized as a regression test** (not just a one-time review): `tests/unit/test_security_hygiene.py` scans all tracked `.py` files under `api/`/`config/` for secret-like assignments, dangerous dynamic execution, and a committed `.env` file, and will fail CI if any of these are (re)introduced.
- **Startup safety summary added:** `GET /safety` reports `paper_trading`, `live_trading`, and an explicit `live_order_code_present: false` — without ever reading or printing secret values (there are none to read).

---

## 12. Configuration safety

Confirmed defaults in `config/settings.py`:

```
PAPER_TRADING = True
LIVE_TRADING = False
```

**These flags are currently dead config — nothing reads them.** This is safe *only* because there is separately no live-order code at all (§8). It is not a substitute for the flags actually being enforced, and should not be relied upon as the safety mechanism going forward if live-order code is ever added. Recommendation (not implemented this session, as it would be premature without any live-order code to gate): any future live-order code path must check `LIVE_TRADING` explicitly and fail closed if the flag is missing/misconfigured, and this should be covered by a test *before* any live-order code is merged.

`config/adaptive_orb.py` (regime/volatility/liquidity/confidence thresholds) is dead config (§4/§6). `config/trading_sessions.py` is dead config except that this audit's F3 fix now reads its `CRYPTO` midnight convention implicitly (via hard-coded UTC-midnight logic, not by importing the dict directly — a documented small inconsistency, low severity, easy follow-up).

No leverage anywhere in the codebase (confirmed — no leverage parameter exists in any order/sizing code). Exchange, symbol, and interval are explicit parameters at every call site touched in this audit (no implicit/global mutable "current instrument" state).

---

## 13. Cross-module invariants (now tested)

| Invariant | Status |
|---|---|
| No order is created without going through `DecisionEngine.decide` → `TradeEngine.execute` | Enforced by construction; no other code path calls `PositionManager.open_position` |
| No `RiskApproval` exists without a valid `TradePlan` | Enforced — `DecisionEngine.decide` rejects incomplete trade plans before computing risk (`test_incomplete_trade_plan_gives_no_trade_not_crash`) |
| No `TradePlan` is created from stale/unclosed data | Enforced upstream at `MarketHub.get_klines` (F1/F10) |
| One signal cannot open two positions | Enforced via signature + session dedup (F11) |
| `NO_TRADE` is a valid, preferred outcome | Default at every gate; verified as the outcome in 6 of 8 e2e scenarios |
| Real trading is unavailable in the test environment | No live-order code exists to call (§8) |
| Restart does not duplicate a trade / does not change economic outcome | Verified for the in-memory state that exists today (F5); **not yet verified for economic outcome**, since no persisted P&L exists yet (F17) |
| A closed trade is immutable | Not yet meaningfully testable — no close-with-PnL path exists yet (F17) |

---

## 14. Code quality notes (small, not all fixed)

- Two parallel architectures coexist: `api/pipeline` (v1, actually used) and `api/pipeline_v2` + `api/core` (`Bootstrap`/`CoreEngine`, entirely unwired — confirmed zero callers). The v2 stack is dead scaffolding; recommend either deleting it or finishing the migration, not both stacks indefinitely.
- `api/main.py` is a second, unused FastAPI app; recommend deleting or clearly marking as deprecated to avoid a future engineer deploying the wrong one.
- `api/market_data.py` is dead/shadowed code (F22); recommend deleting.
- No circular imports found. No obvious thread/process-safety issues found *for the current single-process, single-request-at-a-time usage pattern* — but `PositionManager`/`DailyRiskGuard` class-level mutable state is **not** safe for multiple concurrent workers/processes (e.g., `uvicorn --workers 2`) without an external lock or shared store; flagged as a residual risk for any future deployment change, not fixed here since the current deployment is single-process.
- No infinite loops, no unclosed resources, all HTTP calls in `binance.py`/`bybit.py` use an explicit `timeout=10`.

---

## 15. What changed — file list

```
api/analyzer.py                          — safety try/except wrapper around MarketAnalyzer.analyze
api/binance.py                           — added get_ticker/get_orderbook (were missing, called by provider)
api/bybit.py                             — added get_ticker/get_orderbook (were missing, called by provider)
api/contracts/context.py                 — added LiveContext (visible_market == market for live/paper)
api/data_engine.py                       — (pre-existing WIP diff, unrelated to this audit, left as-is)
api/decision_engine/decision_engine.py   — implemented real .decide() (was missing; Scheduler called it)
api/market_data/market_hub.py            — wired drop_unclosed_candle + validate_candles
api/market_data/candle_utils.py          — NEW: closed-candle filter + candle validation
api/position_manager/position_manager.py — real stateful implementation (was hard-coded no-op)
api/risk_engine.py                       — NaN/inf/negative guards; NEW DailyRiskGuard class
api/scheduler/scheduler.py                — computes indicators, calls .decide(), try/except safety boundary
api/server.py                            — added GET /paper/tick and GET /safety
api/strategy_engine/strategies/orb/orb_strategy.py   — implemented (was an empty stub, ImportError)
api/strategy_engine/strategies/orb/retest.py         — look-ahead fix (visible_market, not market)
api/strategy_engine/strategies/orb/entry.py          — look-ahead fix (visible_market, not market)
api/strategy_engine/strategies/orb/session_open.py   — CRYPTO session now anchored to UTC midnight
api/trade_engine/trade_engine.py         — implemented .execute()/.close() (paper-only) + journal wiring
api/workflow/workflow.py                 — now also calls TradeEngine.execute, returns {decision, execution}
config/settings.py                       — added MIN_RISK_REWARD, MAX_DAILY_TRADES, MAX_DAILY_RISK_PERCENT
pytest.ini, requirements-dev.txt          — NEW: test tooling
tests/**                                  — NEW: 67 tests (unit/integration/regression/e2e)
```

Removed from git tracking (files retained on disk, already covered by `.gitignore`): stale compiled `api/__pycache__/*.pyc`.

---

## 16. Readiness assessment

- **Paper-forward readiness: ~40%.** The core tick path is now safe, deterministic, and covered by tests end-to-end, with real enforcement of one-position, duplicate/session dedup, R:R minimum, NaN-safe risk sizing, daily limits, and closed-candle-only data. But: no regime/liquidity/volatility filtering, no VWAP strategy, no position-exit monitoring loop, ORB opening-range anchoring fix is unvalidated by backtest (none exists), and retest logic is degenerate (F4).
- **Demo-exchange readiness: ~15%.** Needs testnet/demo-account integration, a real exit-monitoring loop, persistence beyond in-process memory, and multi-process-safe state before it is meaningful to run against any exchange's demo environment.
- **Real-money readiness: 0% — BLOCKED.** No live-order code exists (a genuine safety floor), and this must remain blocked pending a separate, explicit user decision regardless of any other progress — see `AUTOTRADING_RELEASE_GATES.md`, Gate 6.

## 17. Five next concrete actions

1. Build the actual `BacktestEngine` loop (currently an empty stub) so ORB/any strategy can be measured at all — nothing about "profitability" can be claimed until this exists (§10).
2. Design and implement genuine multi-bar retest sequencing (F4) and market-regime/volatility/liquidity gating (§4/§6) — both are named requirements with dead config already scaffolded (`config/adaptive_orb.py`), just never wired to a decision.
3. Build the position-exit monitoring loop (F17): live price vs. stored SL/TP, conservative same-candle-SL-and-TP tie-breaking, realized P&L into the journal.
4. Add a max-age/staleness check on the last closed candle (F14) — currently only "is the last candle closed" is checked, not "is it suspiciously old."
5. Resolve the three-way session-boundary inconsistency (F19) into a single source of truth, and decide the fate of the dead `api/pipeline_v2`/`api/core`/`api/market_data.py`/`api/main.py` scaffolding (§14) — delete or finish, not indefinitely both.
