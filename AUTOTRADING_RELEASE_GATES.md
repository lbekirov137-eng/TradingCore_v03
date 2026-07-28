# TradingCore — Release Gates

Each gate requires the stated evidence to exist and pass before the next is attempted. **Gate G (real money) is LOCKED and remains locked regardless of any other progress, pending a separate explicit decision by the user.**

Last evaluated: 2026-07-28 · Test suite: **225 passed, 0 failed, 0 skipped**

---

## GATE A — CODE CORRECTNESS ✅ PASS

**Requires:** all tests green, no hidden skips, no secrets, clean imports, safe defaults.

| Check | Result |
|---|---|
| Test suite | 225 passed / 0 failed / **0 skipped** |
| Unit / integration / regression / e2e | 171 / 29 / 16 / 9 |
| Secrets in repo | None (automated scan, `tests/unit/test_security_hygiene.py`) |
| Dangerous constructs (`eval`/`exec`/`pickle`/shell) | None |
| Default config | `PAPER_TRADING=True`, `LIVE_TRADING=False`, leverage 1, risk 0.1% |
| Suite determinism | Verified across 3 consecutive runs |

---

## GATE B — BACKTEST VALIDITY ❌ **FAIL**

**Requires:** a strategy with a robust, cost-surviving edge on out-of-sample data.

| Strategy | Trades | Net PnL | Walk-forward consistency | Verdict |
|---|---|---|---|---|
| ORB | 7 | +0.37 (fees 2.98) | 57.1% | **FRAGILE** |
| VWAP Trend Pullback | 98 | **−128.37 (−12.8%)** | **0.0%** | **FRAGILE — clearly losing** |

**Why this fails:**
- ORB's sample (n=7) is statistically meaningless, and its net profit is **8× smaller than its own fee bill**. It goes negative under 2× fees, 2× slippage, 3× slippage, and all-costs-×2.
- VWAP has an adequate sample and is decisively unprofitable: profit factor 0.092, **zero** profitable walk-forward windows, 12.8% max drawdown, 13 consecutive losses.

The backtest **infrastructure** is correct and trustworthy (no-look-ahead proven by fault injection — see `AUTOTRADING_BACKTEST_REPORT.md` §5). The **strategies** are not viable. This gate blocks everything downstream.

---

## GATE C — PAPER REPLAY 🟡 PARTIAL

**Requires:** deterministic replay of historical data through the full paper pipeline with reproducible results.

| Check | Result |
|---|---|
| Deterministic e2e replay through `Workflow.run` | ✅ 9 e2e scenarios pass, reproducible |
| Paper broker determinism | ✅ identical inputs → identical final state |
| Restart persistence | ✅ broker, positions, orders, kill switch all survive restart |
| Idempotency on replay | ✅ replaying the same signal never opens a second position |
| **Multi-day continuous replay driving the paper broker** | ❌ **not performed** — the backtest engine and the paper broker are still separate execution paths |

**Blocker:** the backtest engine simulates fills internally rather than routing through `PaperBroker`. A true end-to-end multi-day replay through the live execution path has not been run.

---

## GATE D — 72-HOUR PAPER FORWARD ⛔ NOT STARTED

**Requires:** 72 hours of continuous paper trading on live data, with a complete journal, no crashes, no stuck positions, and correct exit handling.

| Blocker | Detail |
|---|---|
| Gate B fails | No strategy has a demonstrated edge; running 72h would only produce noise |
| No scheduler loop | There is **no background loop**. Each tick is a manually-triggered HTTP request (`GET /paper/tick`). Continuous operation is not yet possible. |
| Exit monitor not wired into a loop | `ExitMonitor` exists and is tested, but nothing calls it on a schedule |

---

## GATE E — BYBIT DEMO ⛔ BLOCKED

**Requires:** Gates A–D pass, then live operation against Bybit's official Demo environment.

| Check | Result |
|---|---|
| Demo adapter implemented | ✅ REST: order create/amend/cancel/query, executions, position, balance |
| Endpoint safety (production hosts rejected) | ✅ enforced per-request, 23 mocked tests |
| Credential handling (no secret leakage) | ✅ boolean flags only |
| Retry/idempotency (`orderLinkId`) | ✅ verified — retries reuse the identical ID |
| **WebSocket + reconnect** | ❌ **not implemented** — REST polling only |
| **Verified against the real demo API** | ❌ **never connected** — all tests are mocked |
| Credentials supplied | ❌ none (by design — awaiting user) |

---

## GATE F — EXTENDED DEMO OBSERVATION ⛔ NOT STARTED

**Requires:** a sustained demo run (recommended ≥ 2 weeks / ≥ 30 trades) with stable reconciliation, no orphaned orders, and PnL matching the exchange's own reporting.

Cannot start before Gate E.

---

## GATE G — MICRO-LIVE REVIEW 🔒 **LOCKED**

**This gate is locked and this work does not, and cannot, unlock it.**

Real-money trading must remain impossible in code unless **all** of the following are true:

1. ✅ Gates A–F all pass — **currently A passes; B fails; C partial; D, E, F not started**
2. ⛔ The user explicitly approves, in a separate decision
3. ⛔ A separate production API key is supplied (never reusing demo keys)
4. ⛔ Withdrawal permission is disabled on that key
5. ⛔ IP restrictions are configured
6. ⛔ A separate micro-live configuration is enabled explicitly
7. ⛔ Risk remains 0.1% per trade
8. ⛔ No leverage
9. ⛔ Maximum initial live exposure is explicitly configured
10. ⛔ The kill switch is tested immediately beforehand

**Current structural reality:** there is **no code path in this repository capable of placing a real order.** `TRADING_ENVIRONMENT=LIVE` raises `ConfigurationError`. Reaching Gate G would require writing new code, which must be separately reviewed and authorized. It is not a configuration flip.

---

## Summary

| Gate | Name | Status |
|---|---|---|
| A | Code correctness | ✅ **PASS** |
| B | Backtest validity | ❌ **FAIL** — both strategies FRAGILE |
| C | Paper replay | 🟡 PARTIAL |
| D | 72-hour paper forward | ⛔ NOT STARTED |
| E | Bybit demo | ⛔ BLOCKED (no WS, never connected, no credentials) |
| F | Extended demo observation | ⛔ NOT STARTED |
| G | Micro-live | 🔒 **LOCKED — requires explicit user decision** |

**The single blocking issue is Gate B.** No amount of infrastructure work substitutes for a strategy with a demonstrated, cost-surviving edge.
