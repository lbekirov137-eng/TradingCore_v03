# TradingCore — Next Actions

Updated 2026-07-28 after Priorities 1–8. Items 2–5 from the prior version of
this document are **done**; the picture on item 1 has gotten clearer (and
worse) with more data and a critical bug fix.

---

## 1. Decide the fate of the strategies (still blocks everything else)

**This is now a much stronger, adequately-sampled finding, not a small-sample guess:**

| Strategy | Scale | Trades | Net PnL | Profit factor | Win rate | Max DD |
|---|---|---|---|---|---|---|
| ORB | 6 weeks | 49 | **−48.59** | 0.174 | 34.7% | 4.96% |
| ORB | 6 months | 208 | **−176.54** | 0.205 | 38.5% | **17.86%** |
| VWAP | 6 weeks | 98 | **−128.37** | 0.092 | 21.4% | 12.84% |

Both strategies are now consistently, robustly unprofitable across two independent time windows and sample sizes. This is a materially different picture than the previous small-sample ORB read (which showed a fluky near-breakeven result on only 7 trades — itself partly an artifact of the same bug described below).

**Concrete options, in order of expected value:**

- **Move to a higher timeframe** (15m/1h) so the per-trade edge is large relative to the ~0.2% round-trip cost. On 5m BTCUSDT, fees alone (`total_fees` ≈ 20–60% of gross loss in every run) consume any plausible edge.
- **Fix the degenerate retest** (ISSUES.md M-4): require a genuine multi-bar pullback after breakout, then a separate confirmation candle, instead of collapsing breakout+retest into one candle.
- **Reject both formulations as currently specified.** A profit factor of 0.09–0.21 across multiple scales is not a tuning problem — it reflects a real, negative expectancy at 5-minute granularity with realistic costs.

**Do not** parameter-sweep on the existing windows to manufacture a better number. Get 12+ months, hold out the final third completely, and don't look at it during development.

---

## 2. ~~Build the scheduler loop~~ ✅ DONE

`api/scheduler/loop.py`: candle-close-aligned, calls `ExitMonitor` automatically every tick, reconciles pending orders, heartbeat, structured logs, Telegram-mock alerts, graceful shutdown. 72-hour paper-forward is now **structurally possible** — it just hasn't been run (see item 6 below for why not yet).

---

## 3. Route the backtest through the paper broker (still open)

**Why:** The backtest still simulates fills internally, so it doesn't exercise `DecisionEngine`'s gates (the 5 guards below, R:R check, duplicate guard). Gate C stays partial until one code path serves both replay and live.

---

## 4. ~~Wire the dormant risk limits into the decision path~~ ✅ DONE

`LossStreakGuard`, `CooldownAfterLossGuard`, `MaxDrawdownGuard`, `MaxTradesPerSessionGuard`, and a new `DailyLossGuard` (realized loss, distinct from the planned-risk `DailyRiskGuard`) are split into separate classes, all wired into `DecisionEngine.decide()`, all registered on trade close by `TradeEngine`. 29 tests prove each one actually blocks a trade with a machine-readable reason.

---

## 5. Make state multi-process safe, then connect Bybit demo (still open)

**Why:** `PositionManager` and all five risk guards use class-level state. Running `uvicorn --workers 2` would give each worker its own independent view of every limit. Fix before any long-running deployment.

Then, and only after Gate B is addressed: supply demo credentials, run `/demo/preflight`, and make the first real connection. Expect schema surprises — never contacted the live API. WebSocket support still needs building.

---

## 6. Why 72-hour paper-forward hasn't been run yet, even though it's now possible

Running it against strategies just shown to be robustly unprofitable at two independent scales would burn 3 days producing confirmation of what the backtests already show, not new evidence. It should follow a decision on item 1, not precede it.

---

## 7. NEW — Fix VWAP's session-VWAP performance (`api/strategy_engine/strategies/vwap/vwap.py`)

`calculate_session_vwap` recomputes cumulative sums from session start on every single call. A 6-week (12k candle) VWAP backtest took roughly 10 minutes; a 6-month run was not attempted this session because of it. Needs an incremental/cached running-sum implementation, keyed per session, to make multi-month VWAP research practical. (ORB's equivalent bottleneck — recomputing EMA/RSI/ATR from full history every candle — was fixed this session via a bounded `indicator_lookback`; the same idea applies here but touches VWAP-specific code, not the shared engine.)

---

## Explicitly deferred

| Item | Why deferred |
|---|---|
| Monte Carlo / confidence intervals | Tooling built and tested; not run at production scale because the direction of the result (both strategies losing) is already unambiguous at current sample sizes |
| Multi-timeframe (1H→15M→5M) cascade | Only matters once a strategy shows an edge |
| Deleting dead code (`pipeline_v2`, `core`, `market_data.py`, `main.py`, `ExchangeRouter`) | Inert, not harmful; needs user decision |
| Real Telegram transport | Mock is sufficient until there is something worth alerting about |
| Gate G (real money) | **LOCKED** — requires explicit user decision, separate production key, and Gates A–F passing |
