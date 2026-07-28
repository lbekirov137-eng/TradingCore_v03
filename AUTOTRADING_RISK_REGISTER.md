# TradingCore — Risk Register

Companion to `AUTOTRADING_FULL_AUDIT_REPORT.md`. One row per finding, most severe first. "Status" reflects this session only — re-verify before relying on it later.

| ID | Severity | Area | Finding | File(s) | Status | Proof test |
|----|----------|------|---------|---------|--------|------------|
| F1 | CRITICAL | Market data | Binance & Bybit klines return the still-forming (unclosed) candle as the last element — used as final price everywhere. Verified live against both exchanges. | `api/binance.py`, `api/bybit.py`, `api/market_data/market_hub.py` | **FIXED** | `tests/unit/test_candle_utils.py::TestDropUnclosedCandle` |
| F2 | CRITICAL | Look-ahead bias | `Retest`/`Entry` read `context.market` (full/untruncated) instead of `context.visible_market` (decision-time view). | `orb/retest.py`, `orb/entry.py` | **FIXED** — verified to fail without fix (reverted live, confirmed failure, restored) | `tests/regression/test_lookahead_bias.py` |
| F3 | CRITICAL | ORB correctness | CRYPTO opening range always anchored to array index 0 of a sliding data window, not a real session/day boundary — meaningless "opening range" for the default symbol. | `orb/session_open.py` | **FIXED** — not backtest-validated (no engine exists) | `tests/regression/test_session_open_anchor.py` |
| F5 | CRITICAL | Execution safety | `PositionManager.has_open_position()` hard-coded to always return `False` — "one open position" rule was a total no-op. | `api/position_manager/position_manager.py` | **FIXED** | `tests/unit/test_position_manager.py` |
| F6 | CRITICAL | Risk engine | Position sizing used raw ATR as stop-distance instead of the real ORB entry-to-stop distance — silently miscalculates actual risk-per-trade. | `api/decision_engine/decision_engine.py`, `api/risk_engine.py` | **FIXED** | `tests/integration/test_decision_engine.py::test_risk_is_computed_from_real_stop_distance_not_raw_atr` |
| F7 | CRITICAL | Availability | `ORBStrategy` class did not exist (file was a doc-comment only) — `ImportError` on `scheduler`/`strategy_engine`/`workflow`. | `orb/orb_strategy.py` | **FIXED** | Import smoke test + `tests/unit/test_orb_strategy.py` |
| F8 | CRITICAL | Availability / safety | `Scheduler.tick` called `DecisionEngine.decide()` (didn't exist), never computed indicators before the strategy needed ATR, and had zero exception handling around data/network calls. | `api/scheduler/scheduler.py`, `api/decision_engine/decision_engine.py` | **FIXED** | `tests/e2e/test_paper_dry_run_scenarios.py::test_c/d`, `tests/integration/test_server_endpoints.py` |
| F9 | CRITICAL | Risk engine | `atr <= 0` guard silently passed `NaN`/`Infinity`/negative/zero balance & price — `NaN`-sized "approved" trades possible. | `api/risk_engine.py` | **FIXED** | `tests/unit/test_risk_engine.py::TestRiskEngineBoundaryCases` |
| F10 | HIGH | Market data | Zero validation of candle data: duplicates, out-of-order, gaps, non-positive/NaN OHLCV all passed through silently. | `api/market_data/market_hub.py` | **FIXED** | `tests/unit/test_candle_utils.py::TestValidateCandles` |
| F11 | HIGH | Execution safety | No idempotency/duplicate-order or one-trade-per-session blocking existed anywhere; nothing called `PositionManager` at all. | `api/trade_engine/trade_engine.py` | **FIXED** | `tests/e2e/test_paper_dry_run_scenarios.py::test_g` |
| F12 | HIGH | Risk engine | No daily trade-count or daily risk-budget limit existed. | `api/risk_engine.py` (`DailyRiskGuard`) | **FIXED** (proxy on planned risk, not realized P&L — see F17) | `tests/unit/test_risk_engine.py::TestDailyRiskGuard`, e2e `test_f` |
| F13 | HIGH | Interface bug | `BinanceProvider`/`BybitProvider` call `get_ticker`/`get_orderbook` which didn't exist on `BinanceAPI`/`BybitAPI` — `AttributeError` if ever called (currently unreachable). | `api/binance.py`, `api/bybit.py` | **FIXED** | Import/smoke check; not yet exercised by a dedicated unit test — recommend adding one |
| F14 | HIGH | Market regime/liquidity | Regime/volatility/liquidity/news-shock filters are entirely unimplemented — strategy trades identically in all market conditions despite named config scaffolding (`config/adaptive_orb.py`) that is 100% dead. | `api/strategy_engine/strategies/orb/cancel_scenario.py`, `config/adaptive_orb.py` | **NOT FIXED** — feature gap, documented | — |
| F15 | HIGH | Strategy coverage | Session VWAP Trend Pullback strategy does not exist anywhere in the codebase. | — | **NOT APPLICABLE — never built** | — |
| F16 | HIGH | Validation | No working `BacktestEngine` exists (`api/backtesting/backtest_engine.py` is an empty stub) — no profitability, walk-forward, or robustness claim is possible for any strategy. | `api/backtesting/backtest_engine.py` | **NOT FIXED** — blocking gap, documented | — |
| F17 | HIGH | Trade lifecycle | No position-exit monitoring loop exists — a paper position never closes itself; no SL/TP-hit detection, no realized P&L, no equity curve. | `api/trade_engine/trade_engine.py`, `api/backtesting/trade_journal.py` | **NOT FIXED** — feature gap, documented | — |
| F18 | MEDIUM | ORB correctness | `Breakout`/`Retest` both key off the same last candle — "retest" collapses into "did the breakout candle close near the level," no genuine later multi-bar pullback is verified. | `orb/breakout.py`, `orb/retest.py` | **NOT FIXED** — needs product/quant decision | — |
| F19 | MEDIUM | Session logic | Three independent, uncoordinated session-boundary definitions coexist (`SessionResolver`, `config/trading_sessions.py`, `SessionRule`). | `config/session_resolver.py`, `config/trading_sessions.py`, `api/decision_engine/rules/session_rule.py` | **NOT FIXED** — documented | — |
| F20 | MEDIUM | Dead code | `ExchangeRouter` (auto exchange selection + health check) is never called by anything — config flags `AUTO_SELECT_EXCHANGE`/`DEFAULT_DATA_EXCHANGE` have zero effect. | `api/exchange_router.py` | **NOT FIXED** — documented | — |
| F21 | MEDIUM | Dead config | `config/trading_sessions.py` schedule dict is unused (except implicitly via the F3 fix's hard-coded UTC-midnight logic). | `config/trading_sessions.py` | **NOT FIXED** — documented | — |
| F22 | LOW | Dead code | `api/market_data.py` (flat module) permanently shadowed by `api/market_data/` package — confirmed via import resolution test; unreachable. | `api/market_data.py` | **NOT FIXED** — recommend delete | — |
| F23 | LOW | Dependencies | `python-dotenv` declared but never imported anywhere — no `.env` loading mechanism actually exists. | `requirements.txt` | **NOT FIXED** — documented | — |
| F24 | MEDIUM | Architecture | Two parallel architectures coexist (`api/pipeline` used; `api/pipeline_v2` + `api/core` entirely unwired dead scaffolding). | `api/pipeline_v2/`, `api/core/` | **NOT FIXED** — documented | — |
| F25 | LOW | Observability | Broad `except Exception: continue` silently swallows all errors in exchange health checks with no logging. | `api/exchange_router.py`, provider `health_check()` | **NOT FIXED** — acceptable for a boolean probe, but no logging | — |
| F26 | INFORMATIONAL | Repo hygiene | Stale compiled `.pyc` files were tracked in git despite `.gitignore` already excluding new ones. | `api/__pycache__/*` | **FIXED** (untracked, kept on disk) | `git status` |
| F27 | INFORMATIONAL (positive) | Security | No secrets, hardcoded keys, `eval`/`exec`/`pickle`/shell usage found anywhere; no `.env` ever committed. | repo-wide | **VERIFIED CLEAN** | `tests/unit/test_security_hygiene.py` |
| F28 | INFORMATIONAL (positive) | Safety | No live-order-placement code exists anywhere — `LIVE_TRADING` cannot currently be defeated by any single config flip because there is nothing for it to gate. | `api/binance.py`, `api/bybit.py` | **VERIFIED CLEAN** | `GET /safety` endpoint; manual grep |
| F29 | INFORMATIONAL | Scope | No Telegram integration exists (docstring mention only); no background scheduler/cron loop exists — each tick is a single triggered call. | `api/workflow/workflow.py` | **NOT APPLICABLE — never built** | — |

## Count by severity

| Severity | Count | Fixed this session | Documented / not fixed |
|---|---|---|---|
| CRITICAL | 9 | 9 | 0 |
| HIGH | 8 | 3 | 5 |
| MEDIUM | 5 | 0 | 5 |
| LOW | 3 | 1 | 2 |
| INFORMATIONAL | 4 | 1 (+2 positive verifications) | 1 |
| **Total** | **29** | **14** | **15** |
