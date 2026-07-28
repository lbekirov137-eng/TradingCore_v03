# TradingCore — Open Issues

Ordered by severity. Issues fixed during this work are in `AUTOTRADING_RISK_REGISTER.md`.

---

## CRITICAL

### C-1 — Neither strategy has a demonstrated edge (blocks everything)
- **Evidence:** ORB n=7, net +0.37 against 2.98 in fees, FRAGILE under 2× fees. VWAP n=98, net −128.37 (−12.8%), profit factor 0.092, **0% walk-forward consistency**.
- **Impact:** Gate B fails. Paper-forward and demo are pointless until a strategy survives its own cost base.
- **Fix direction:** Either raise the per-trade edge (larger stop distance / higher timeframe so fees are a smaller fraction) or abandon these formulations. Do **not** parameter-tune on the same 6-week window — that manufactures overfitting.

### C-2 — No background scheduler loop exists
- **Evidence:** `Scheduler.tick()` is called once per HTTP request. There is no cron, no async loop, no daemon.
- **Impact:** Continuous paper-forward (Gate D) and demo operation (Gate E) are **impossible** today. The exit monitor is never called automatically, so a position opened via `/paper/tick` will never be closed unless someone triggers it manually.
- **Fix direction:** Add an asyncio loop or external scheduler that calls `Scheduler.tick()` then `ExitMonitor.check()` on each closed candle.

---

## HIGH

### H-1 — Exit monitor is not wired into any running loop
- `ExitMonitor` is implemented and tested (13 tests) but nothing invokes it on a schedule. Consequence of C-2.

### H-2 — Backtest and paper broker are separate execution paths
- The backtest simulates fills internally rather than routing through `PaperBroker`. Backtest results therefore do **not** validate the code that would actually execute in paper/demo, and they bypass `DecisionEngine` (daily limits, kill switch, R:R gate, duplicate guard).
- **Impact:** Gate C only partially satisfied.

### H-3 — Bybit demo adapter has never contacted the real API
- All 23 tests are mocked. Field names and status strings come from Bybit V5 docs, not observed responses. First real connection may reveal schema mismatches.

### H-4 — No WebSocket client; REST polling only
- The spec requires "WebSocket reconnect + REST fallback". Only REST exists. `websockets` is installed but unused.

### H-5 — Class-level mutable state is not multi-process safe
- `PositionManager`, `DailyRiskGuard`, `LossStreakGuard`, and the module-level `broker`/`idempotency_store` singletons use class/module state. Running `uvicorn --workers 2` would give each worker its own independent view of "one open position" and daily limits.
- **Impact:** Silent limit bypass under a routine deployment change.

### H-6 — `LossStreakGuard` is implemented but not wired into the decision path
- Cooldown-after-loss, consecutive-loss limit, max-drawdown stop, and max-trades-per-session all exist in `api/risk_engine.py` but `DecisionEngine.decide` does not call `LossStreakGuard.check()`, and nothing calls `register_result()` on trade close.
- **Impact:** These Phase 7 limits are currently inert.

---

## MEDIUM

### M-1 — VWAP session recalculation is O(n²)
- `calculate_session_vwap` recomputes from session start on every candle; a 12k-candle backtest takes minutes. Needs incremental accumulation.

### M-2 — VWAP backtest numbers predate the regime filters
- Reported VWAP results were produced before filters were wired in. Verdict is unaffected (filters cannot invert a 0.09 profit factor) but the numbers are not apples-to-apples.

### M-3 — Higher-timeframe confirmation is approximated
- The VWAP strategy's "1H → 15M → 5M" requirement is approximated by EMA structure on the working timeframe. No multi-timeframe data loading exists.

### M-4 — ORB retest is degenerate
- `Breakout` and `Retest` both key off the same final candle, so "retest" collapses into "did the breakout candle close near the level". No genuine multi-bar pullback is verified. Carried over unfixed from the original audit (F18).

### M-5 — Spread is not modeled in paper mode
- `check_spread(None)` returns allowed. Real spread data is not fetched, so the spread limit is inert in paper trading.

### M-6 — No realized-PnL feedback into risk limits
- `DailyRiskGuard` tracks *planned* risk at open time, not realized losses. A true "max daily loss" limit does not exist.

### M-7 — Duplicate/dead architecture still present
- `api/pipeline_v2/`, `api/core/` (unwired), `api/market_data.py` (shadowed by the package), `api/main.py` (second unused FastAPI app), `ExchangeRouter` (zero callers). Carried over from the original audit.

---

## LOW

### L-1 — `python-dotenv` declared but never imported
- `.env` is documented but nothing loads it. Environment variables must currently be set in the shell.

### L-2 — Single symbol, single market regime tested
- Six weeks of BTCUSDT only. No bull/bear/high-volatility regime separation.

### L-3 — Funding costs not modeled
- Irrelevant for spot; would matter if perpetuals are ever used.

### L-4 — `starlette` TestClient deprecation warning
- Cosmetic; suggests installing `httpx2`.

---

## Resolved during this phase

See `AUTOTRADING_RISK_REGISTER.md` for the full list. Highlights: ORB session anchoring, look-ahead in retest/entry, NaN/Inf risk acceptance, hard-coded position lock, missing `ORBStrategy`, unclosed-candle repainting, three-way session-definition inconsistency, missing idempotency, missing exit monitor, missing kill switch, missing backtest engine.
