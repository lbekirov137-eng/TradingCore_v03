# Pre-Existing Test Failures — Read-Only Triage

**Branch:** `reconcile-railway-paper` @ `21c6825`
**Full run:** `38 failed, 167 passed`
**Status of this document:** READ-ONLY analysis. **No test and no production code was modified.**

All 38 failures listed here exist identically on `origin/main` (verified by diffing exact test IDs: the set difference between this branch and the baseline is empty). None were introduced by the reconciliation work.

---

## Categories used

| Code | Meaning |
|---|---|
| **A** | Outdated/incorrect test (asserts stale wording or stale behavior) |
| **B** | Real defect in working code |
| **C** | Old-vs-new architecture mismatch (test written against a superseded contract) |
| **D** | Import / fixture / test-infrastructure error |
| **E** | Ambiguous — needs separate investigation |

---

## Summary counts

| Category | Count | Share |
|---|---|---|
| **C** — architecture mismatch | **24** | 63% |
| **A** — outdated test assertions | **12** | 32% |
| **B** — real code defect | **1** | 3% |
| **E** — ambiguous | **1** | 3% |
| **D** — infrastructure | **0** | 0% |
| **Total** | **38** | |

---

## Root causes (3 clusters explain 37 of 38)

### Cluster 1 — `selected_trade` contract change (24 failures, Category C)

The coordinator introduced `StrategyCoordinatorStep`, which writes a **nested** structure:

```python
context.strategy["selected_trade"] = {"signal": ..., "entry": ..., "stop": ..., "strategy": ...}
```

`RiskStep` and `TradePlanStep` were updated to consume it and now raise `TypeError: ... selected_trade must be dict` when it's absent. The affected tests still build the **old flat** shape:

```python
context.strategy = {"signal": "BUY"}     # superseded contract
```

The production chain is internally coherent (coordinator produces → steps consume). The tests simply predate it.

### Cluster 2 — error-message wording changed (12 failures, Category A)

Tests assert older message text than the code now emits. Examples:

| Expected by test | Actually raised |
|---|---|
| `ATR must be greater than zero` | `RiskStep ATR must be a positive finite number` |
| `market price is missing` | `RiskStep price must be a positive finite number` |
| `DecisionEngine result missing field: failed_rules` | `DecisionEngine missing field: failed_rules` |
| `PaperExecutionStep supports BUY only` | `PaperExecutionStep supports BUY and SELL only` |

Note: the newer messages are generally **more precise** (e.g. "positive finite number" also covers NaN/Inf, which the older wording did not).

### Cluster 3 — `market_snapshot` required fields (3 failures, Category C)

`ValueError: market_snapshot is missing fields: interval, open_times_ms, opens` — the unified market context now requires fields the older integration fixtures don't supply.

---

## Full failure table

### `tests/test_risk_step.py` — 10 failures

| # | Test | Module under test | Actual | Expected | Root cause | Cat | Recommendation |
|---|---|---|---|---|---|---|---|
| 1 | `test_buy_signal_is_approved` | RiskStep | `TypeError: selected_trade must be dict` | approved risk | old flat `strategy` shape | C | Rewrite fixture to new contract |
| 2 | `test_no_trade_signal_is_blocked` | RiskStep | same | blocked | same | C | Rewrite fixture |
| 3 | `test_sell_signal_is_blocked_in_spot_long_only_mode` | RiskStep | same | SELL blocked | same | C | Rewrite fixture — **keep this test, it guards spot-long-only** |
| 4 | `test_default_portfolio_values_are_used` | RiskStep | same | defaults applied | same | C | Rewrite fixture |
| 5 | `test_invalid_strategy_signal_is_rejected` | RiskStep | same | rejection | same | C | Rewrite fixture |
| 6 | `test_excessive_risk_percent_is_rejected` | RiskStep | same | rejection | same | C | Rewrite fixture — **risk-limit guard** |
| 7 | `test_incomplete_risk_result_is_rejected` | RiskStep | same | rejection | same | C | Rewrite fixture |
| 8 | `test_missing_price_is_rejected` | RiskStep | `RiskStep price must be a positive finite number` | `market price is missing` | wording | A | Update assertion |
| 9 | `test_zero_atr_is_rejected` | RiskStep | `RiskStep ATR must be a positive finite number` | `ATR must be greater than zero` | wording | A | Update assertion |
| 10 | `test_non_dictionary_risk_result_is_rejected` | RiskStep | `selected_trade must be dict` | `RiskEngine.calculate() must return dict` | contract check now fires first | C | Rewrite fixture |

### `tests/test_trade_plan_step.py` — 10 failures

| # | Test | Actual | Root cause | Cat | Recommendation |
|---|---|---|---|---|---|
| 11 | `test_approved_buy_creates_trade_plan` | `TypeError: selected_trade must be dict` | old contract | C | Rewrite fixture |
| 12 | `test_blocked_risk_creates_no_trade_plan` | same | old contract | C | Rewrite fixture |
| 13 | `test_missing_risk_permission_is_rejected` | same | old contract | C | Rewrite fixture |
| 14 | `test_approved_risk_requires_buy_signal` | same | old contract | C | Rewrite fixture |
| 15 | `test_missing_market_price_is_rejected` | same | old contract | C | Rewrite fixture |
| 16 | `test_zero_atr_is_rejected` | same | old contract | C | Rewrite fixture |
| 17 | `test_missing_trade_plan_field_is_rejected` | same | old contract | C | Rewrite fixture |
| 18 | `test_inconsistent_buy_levels_are_rejected` | same | old contract | C | Rewrite fixture — **consistency guard** |
| 19 | `test_invalid_risk_reward_is_rejected` | same | old contract | C | Rewrite fixture — **R:R guard** |
| 20 | `test_non_dictionary_trade_plan_is_rejected` | `selected_trade must be dict` vs `TradePlan.build() must return dict` | contract check fires first | C | Rewrite fixture |

### `tests/test_paper_execution_step.py` — 9 failures

| # | Test | Actual | Expected | Cat | Recommendation |
|---|---|---|---|---|---|
| 21 | `test_trade_creates_simulated_filled_order` | `ValueError: supports BUY and SELL only` | filled order | C | Rewrite fixture (signal arrives `None` under new contract) |
| 22 | `test_no_trade_creates_skipped_order` | dict now has extra `side`/`signal`/`strategy` keys | old dict shape | C | Update expected dict |
| 23 | `test_sell_signal_is_rejected` | `supports BUY and SELL only` | `supports BUY only` | C | **See safety note below** |
| 24 | `test_invalid_final_decision_is_rejected` | `Invalid final decision: WAIT` | `PaperExecutionStep invalid final decision: WAIT` | A | Update assertion |
| 25 | `test_non_dictionary_decision_is_rejected` | `decision must be dict` | `expected context.decision to be dict` | A | Update assertion |
| 26 | `test_disallowed_trade_plan_is_rejected` | `supports BUY and SELL only` | `trade plan is not allowed` | A | Update assertion + fixture |
| 27 | `test_missing_trade_plan_field_is_rejected` | `supports BUY and SELL only` | `missing field: position_size` | A | Update assertion + fixture |
| 28 | `test_zero_position_size_is_rejected` | `supports BUY and SELL only` | `'position_size' must be a positive finite number` | A | Update assertion + fixture |
| 29 | `test_inconsistent_buy_levels_are_rejected` | `supports BUY and SELL only` | `BUY levels are inconsistent` | A | Update assertion + fixture |

### `tests/test_decision_step.py` — 5 failures

| # | Test | Actual | Expected | Cat | Recommendation |
|---|---|---|---|---|---|
| 30 | `test_all_approvals_produce_trade` | `NO_TRADE` | `TRADE` | C | Rewrite fixture to new contract |
| 31 | `test_non_buy_signal_blocks_trade` | `Selected signal is not BUY or SELL: None` | `Strategy signal is not BUY: NO TRADE` | C | Rewrite fixture |
| 32 | `test_missing_decision_field_is_rejected` | `DecisionEngine missing field: failed_rules` | `DecisionEngine result missing field: ...` | A | Update assertion |
| 33 | `test_unknown_engine_decision_is_rejected` | `Invalid DecisionEngine decision` | `DecisionEngine returned invalid decision: WAIT` | A | Update assertion |
| 34 | **`test_invalid_confidence_is_rejected`** | **DID NOT RAISE** | `ValueError` for `confidence=1.5` | **B** | **Fix the code — see below** |

### `tests/test_pipeline_v2_integration.py` / `test_pipeline_paper_execution.py` — 3 failures

| # | Test | Actual | Cat | Recommendation |
|---|---|---|---|---|
| 35 | `test_complete_pipeline_produces_trade` | `market_snapshot is missing fields: interval, open_times_ms, opens` | C | Extend fixture |
| 36 | `test_decision_engine_can_block_complete_pipeline` | same | C | Extend fixture |
| 37 | `test_complete_pipeline_creates_paper_order` | same | C | Extend fixture |

### `tests/test_bootstrap.py` — 1 failure

| # | Test | Actual | Cat | Recommendation |
|---|---|---|---|---|
| 38 | `test_bootstrap_registers_expected_modules_in_order` | Registered order now includes `market_intelligence`, `trade_plan`, `paper_execution`, coordinator steps | E | **Investigate** — is the new order intentional and correct? Then update the test to the agreed order |

---

## The one real code defect (Category B)

**#34 `test_invalid_confidence_is_rejected` — confidence range validation was lost.**

`api/pipeline_v2/steps/decision_step.py::_validate_engine_decision` checks only that `confidence` is **present**:

```python
for field in ("decision", "score", "confidence", "failed_rules", "reason"):
    if field not in decision:
        raise ValueError(f"DecisionEngine missing field: {field}")
```

There is no range check, so `confidence = 1.5` (or `-3`, or `100`) passes silently. The test correctly expects rejection.

**This is a test that is right and code that is wrong** — the opposite of the other 37. It should be fixed in the **code**, not by deleting the test.

**Severity for PAPER:** low-but-real. No money moves in PAPER, but an out-of-range confidence can distort decision scoring and pollute the very statistics this PAPER run exists to collect.

---

## Safety note on #23 (`test_sell_signal_is_rejected`)

This test asserts `PaperExecutionStep supports BUY only`. The code now says `BUY and SELL`. **This is not an unguarded safety regression** — SELL is gated twice:

- `RiskStep`: `elif signal == "SELL" and mode != PAPER_LONG_SHORT_MODE:` → blocked
- `PaperExecutionStep`: `if signal == "SELL" and plan.get("execution_mode") != "PAPER_LONG_SHORT":` → raises

And the default is `DEFAULT_EXECUTION_MODE = "SPOT_LONG_ONLY"`, only overridable via the `TRADING_EXECUTION_MODE` env var, which is **not set** in the Railway PAPER variable set.

So spot-long-only holds by default. The test is outdated relative to a deliberate, mode-gated capability — but **the capability itself deserves your explicit sign-off** before anyone sets `TRADING_EXECUTION_MODE=PAPER_LONG_SHORT`.

---

## Which failures are critical for PAPER?

**None block a Railway PAPER launch.** Justification:

| Concern | Status |
|---|---|
| All 38 exist on `origin/main` too | Not introduced here |
| All 38 are in `tests/`, none in a runtime path | No production behavior depends on them |
| 37 of 38 are tests lagging behind the code | Not evidence of broken production code |
| The 1 real defect (#34) | Affects decision-score hygiene, not order safety; no real money in PAPER |
| Live-safety invariants | Independently verified green — see below |

**Independently verified after this triage (unchanged):** PAPER mode ✅ · `max_leverage = 1` ✅ · `live_trading = false` ✅ · `risk = 0.001` ✅ · no order-placement methods ✅ · `/health` 200 ✅ · `/ready` 200 READY ✅ · `/safety` consistent ✅

**Caveat worth stating plainly:** 24 of these tests were *guarding real safety properties* (risk limits, R:R validation, spot-long-only, level consistency). They are currently **not protecting anything**, because they fail for a contract reason before reaching their assertion. The system is not less safe than `origin/main` — but this is meaningfully less test coverage than the passing count suggests.

---

## Recommended fix order (small blocks)

| Block | Scope | Failures | Risk | Why this order |
|---|---|---|---|---|
| **1** | Fix confidence range validation in `DecisionStep` | 1 (#34) | Low | Only Category B; code fix; smallest and highest-value |
| **2** | Update wording-only assertions | 6 (#8, #9, #24, #25, #32, #33) | Very low | Pure assertion text; no fixture work; verify new wording is *better* before accepting |
| **3** | Build one shared `selected_trade` fixture helper | 0 | Low | Groundwork — one helper unblocks blocks 4–5 |
| **4** | Migrate `test_risk_step.py` to new contract | 8 | Medium | Restores risk-limit + spot-long-only coverage |
| **5** | Migrate `test_trade_plan_step.py` | 10 | Medium | Restores R:R and level-consistency coverage |
| **6** | Migrate `test_paper_execution_step.py` | 9 | Medium | Needs the #23 SELL decision first |
| **7** | Extend `market_snapshot` integration fixtures | 3 | Medium | Integration-level, slower feedback |
| **8** | Resolve `test_bootstrap` module order (Category E) | 1 | Low | Requires deciding the *intended* pipeline order first |

Rule for every block: fix → run that file → run full suite → confirm the count only ever decreases and no new IDs appear.

---

## Decisions needed from you before any fixing starts

1. **#34** — confirm confidence must be range-validated (I believe yes; it is a real lost guard).
2. **#23 / SELL** — confirm `PAPER_LONG_SHORT` is an intended capability and stays **opt-in only**.
3. **#38 / bootstrap order** — what *is* the intended module registration order? The test and code disagree and I cannot tell which is authoritative.
4. **General principle** — when a test's expectation and the code disagree on *wording*, do you want the assertion updated, or the message reverted to the older text? I recommend updating assertions, since the newer messages are more precise, but that is a judgement call you should own.

---

## GO / NO-GO for Railway PAPER before fixing all 38

🟢 **GO.**

These 38 are a **test-debt** problem, not a runtime-safety problem. They are inherited from `origin/main`, live entirely in `tests/`, and every live safety invariant is independently verified green. Blocking the PAPER launch on them would delay collecting the honest forward statistics this stage exists to gather, with no safety benefit.

🔴 **LIVE trading: NO-GO** — unchanged and unconditional.

The one caveat to carry forward: **do not treat "167 passed" as full safety coverage.** Roughly two dozen safety-guarding tests are currently inert, and blocks 4–6 above are what restore them.

---

*Nothing in this document has been applied. No test, production file, or configuration was modified during this triage.*
