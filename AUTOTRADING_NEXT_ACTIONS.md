# TradingCore — Next Actions

Ordered by what actually unblocks progress. Everything below Gate B is wasted effort until Gate B is addressed.

---

## 1. Decide the fate of the strategies (blocks everything else)

**Why first:** Gate B fails. ORB nets +0.37 against 2.98 in fees on 7 trades; VWAP loses 128 USDT over 98 trades with 0% walk-forward consistency. No infrastructure work changes this.

Concrete options, in order of expected value:

- **Move to a higher timeframe** (15m/1h) so the per-trade edge is large relative to the ~0.2% round-trip cost. On 5m BTCUSDT, fees consume the entire ORB edge.
- **Fix the degenerate retest** (ISSUES.md M-4): require a genuine multi-bar pullback after breakout, then a separate confirmation candle. The current implementation collapses breakout+retest into one candle.
- **Reject VWAP as formulated.** Profit factor 0.092 with an adequate sample is not a tuning problem.

**Do not** parameter-sweep on the existing 6-week window. That manufactures the overfitting this project exists to avoid. Download 6–12 months first, hold out the final third, and never look at it during development.

---

## 2. Build the scheduler loop

**Why:** There is no background loop (ISSUES.md C-2). `/paper/tick` must be triggered by hand, and `ExitMonitor` is never called automatically — so a paper position opened today would stay open forever.

Required shape:
```
every closed candle:
    Scheduler.tick(context)      # data → filters → strategy → decision → entry
    ExitMonitor.check(candle)    # SL / TP / invalidation / stale
    reconciler.reconcile_all_pending()
    health.heartbeat()
```
Without this, Gates D, E, and F are all structurally impossible.

---

## 3. Route the backtest through the paper broker

**Why:** The backtest simulates fills internally (ISSUES.md H-2), so it validates neither `PaperBroker` nor `DecisionEngine` (daily limits, kill switch, R:R gate, duplicate guard are all bypassed). Gate C stays partial until one code path serves both replay and live.

This also makes a multi-day deterministic replay through the real execution path possible — the missing piece of Gate C.

---

## 4. Wire the dormant risk limits into the decision path

**Why:** `LossStreakGuard` (cooldown after loss, consecutive-loss limit, max-drawdown stop, max-trades-per-session) is implemented and tested but **never called** (ISSUES.md H-6). These Phase 7 limits are currently inert.

Needs: `DecisionEngine.decide()` calls `LossStreakGuard.check()`; `TradeEngine.close()` calls `register_result()` with realized PnL. Also add a real max-daily-*loss* limit (M-6) — the current daily guard tracks planned risk, not realized losses.

---

## 5. Make state multi-process safe, then connect Bybit demo

**Why:** `PositionManager`, `DailyRiskGuard`, and the broker/idempotency singletons are class-level state (ISSUES.md H-5). Running `uvicorn --workers 2` — a routine change — would give each worker its own "one open position" counter and silently bypass every limit. Fix before any long-running deployment.

Then, and only after Gates B–D pass: supply demo credentials, run `/demo/preflight`, and make the first real connection. Expect schema surprises — the adapter has never contacted the live API (H-3). WebSocket support still needs building (H-4).

---

## Explicitly deferred

| Item | Why deferred |
|---|---|
| Monte Carlo / confidence intervals | Meaningless at ORB's n=7; VWAP already unambiguous |
| Multi-timeframe (1H→15M→5M) cascade | Only matters once a strategy shows an edge |
| Deleting dead code (`pipeline_v2`, `core`, `market_data.py`, `main.py`, `ExchangeRouter`) | Inert, not harmful; needs user decision |
| Real Telegram transport | Mock is sufficient until there is something worth alerting about |
| Gate G (real money) | **LOCKED** — requires explicit user decision, separate production key, and Gates A–F passing |
