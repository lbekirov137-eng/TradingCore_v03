# TradingCore — Backtest Report

**Generated:** 2026-07-28
**Engine:** `api/backtesting/backtest_engine.py` (deterministic, no-look-ahead, costs modeled)
**Data:** BTCUSDT 5m, 11,999 closed candles, 2026-06-16T13:35Z → 2026-07-28T05:25Z (~6 weeks), Binance public read-only endpoint.

> **This report makes no claim of profitability. Both strategies are classified FRAGILE and neither is approved for paper-forward.**

---

## 1. Methodology

The engine enforces the following, each covered by tests in `tests/regression/test_backtest_no_lookahead.py`:

| Property | Implementation |
|---|---|
| No look-ahead | Strategy sees `visible_market` truncated to `[0..i]`; verified by deliberately injecting a look-ahead bug and confirming 3 tests fail (see §5) |
| Decision/execution separation | Decision made on **closed** candle `i`; entry fills on candle `i+1` **at its open**, never at the decision candle's close |
| Exit timing | Exit never occurs on the entry candle |
| SL+TP in same candle | Resolves to **STOP_LOSS** — the profitable outcome is never auto-selected (intrabar order is unknowable from OHLC) |
| Costs | Fee (0.1%/side), slippage (5 bps/side), spread (2 bps, half per side) all applied to entry and exit |
| Unresolved trades | Trades open at end of data are flagged `END_OF_DATA` / `UNRESOLVED` and contribute **zero** PnL — never closed favourably |
| Determinism | Identical inputs produce byte-identical reports (asserted in tests) |

**Cost configuration (`BacktestConfig`):** `fee_rate=0.001`, `slippage_bps=5.0`, `spread_bps=2.0`, `risk_percent=0.1`, `initial_balance=1000`.

---

## 2. ORB strategy — results

Regime/liquidity/data-quality filters **active** (`api/strategy_engine/filters/regime.py`).

### Full period (11,999 candles)

| Metric | Value |
|---|---|
| Total trades | **7** |
| Unresolved | 1 |
| Wins / Losses | 5 / 2 |
| Win rate | 71.43% |
| Net PnL | **+0.367 USDT** (+0.037%) |
| Gross profit / loss | 3.324 / 2.957 |
| Total fees | **2.981** |
| Profit factor | 1.124 |
| Expectancy per trade | +0.052 |
| Average win / loss | 0.665 / 1.479 |
| Max drawdown | 2.602 (0.26%) |
| Final balance | 1000.37 |

### Walk-forward (7 windows)

| Metric | Value |
|---|---|
| Windows | 7 |
| Windows with trades | 7 |
| Profitable windows | 4 |
| **Consistency** | **57.14%** |

### Sensitivity / stress

| Scenario | Trades | Net PnL | Win rate |
|---|---|---|---|
| baseline | 7 | **+0.367** | 71.4% |
| fees ×2 | 7 | **−2.610** | 42.9% |
| slippage ×2 | 7 | **−0.945** | 71.4% |
| slippage ×3 | 7 | **−2.337** | 42.9% |
| spread ×2 | 7 | +0.106 | 71.4% |
| latency +1 candle | 7 | +0.579 | 71.4% |
| **all costs ×2** | 7 | **−4.248** | 28.6% |

**Verdict: FRAGILE.** The entire edge is smaller than the fee bill (net +0.37 vs. fees 2.98). Doubling fees — a realistic scenario on a different fee tier — turns the result decisively negative.

---

## 3. Session VWAP Trend Pullback — results

*(Run before regime filters were wired in; the filters have since been added and would reduce trade count. Re-run required — see Known Limitations.)*

### Full period

| Metric | Value |
|---|---|
| Total trades | **98** |
| Wins / Losses | 21 / 77 |
| Win rate | **21.43%** |
| Net PnL | **−128.365 USDT (−12.8%)** |
| Gross profit / loss | 13.00 / 141.37 |
| Total fees | 74.04 |
| Profit factor | **0.092** |
| Expectancy per trade | −1.310 |
| Max drawdown | 128.37 (**12.84%**) |
| Max consecutive losses | **13** |

### Walk-forward

| Metric | Value |
|---|---|
| **Consistency** | **0.00%** — zero profitable windows |

### Sensitivity / stress

| Scenario | Trades | Net PnL | Win rate |
|---|---|---|---|
| baseline | 98 | −128.365 | 21.4% |
| fees ×2 | 98 | −195.340 | 9.2% |
| slippage ×2 | 98 | −132.797 | 16.3% |
| slippage ×3 | 98 | −136.759 | 9.2% |
| spread ×2 | 98 | −129.262 | 20.4% |
| latency +1 candle | 96 | −167.679 | 15.6% |
| all costs ×2 | 98 | −185.307 | 6.1% |

**Verdict: FRAGILE — decisively unprofitable.** Unlike ORB, this has a meaningful sample (98 trades) and is unambiguously losing: profit factor 0.09, zero profitable walk-forward windows, 12.8% drawdown, 13 consecutive losses (which would trip the consecutive-loss limit and the max-drawdown stop in live operation).

---

## 4. Statistical honesty

- **ORB's 7 trades are statistically meaningless.** No confidence interval, expectancy estimate, or profitability claim is defensible at n=7. A single trade outcome flips the sign of the result.
- **ORB's edge is smaller than its cost base.** Net +0.37 against 2.98 in fees means the strategy is essentially trading for the exchange's benefit.
- **VWAP has an adequate sample and fails clearly.** n=98 with PF 0.092 and 0% walk-forward consistency is sufficient evidence to reject the current implementation.
- Monte Carlo resampling and formal confidence intervals were **not** run — with n=7 (ORB) they would be meaningless, and with VWAP the result is already unambiguous.
- Neither strategy's parameters were tuned on this data. **No parameter optimization was performed**, deliberately: optimizing on a 6-week window would manufacture exactly the overfitting this audit exists to prevent.

---

## 5. Proof the no-look-ahead tests actually work

The look-ahead protection was verified by **deliberately breaking it** rather than assuming it works. `BacktestContext.visible_market` was patched to expose the full market instead of `[0..index]`:

```
3 failed, 4 passed
  FAILED test_strategy_never_sees_more_than_current_index
  FAILED test_no_future_candle_is_ever_visible
  FAILED test_appending_future_candles_does_not_change_past_decisions
      assert [107.9, ...] == [104.9, ...]   # past decisions changed
```

The fix was restored and all 7 tests pass again. The strictest test — *appending future candles must not change past decisions* — is the one that would catch subtle leakage.

---

## 6. Known limitations

1. **VWAP results predate the regime filters.** Numbers above reflect the unfiltered strategy; a re-run is required for an apples-to-apples comparison. This does not change the verdict — filters reduce trade count, they don't invert a 0.09 profit factor.
2. **VWAP backtest is O(n²)** — session VWAP is recomputed from session start on every candle. ~12k candles takes several minutes. Needs incremental computation before larger studies.
3. **No order-book modeling.** Partial fills in backtest are approximated; spread is a flat assumption, not real bid/ask.
4. **Single symbol, single regime window.** Six weeks of BTCUSDT covers one broad market character. Bull/bear/high-volatility regime separation was not performed — insufficient history downloaded.
5. **The backtest calls strategies directly**, bypassing `DecisionEngine`. It therefore does **not** exercise the daily-trade limit, kill switch, duplicate-order guard, or R:R gate. Those are covered by separate integration/e2e tests, but the backtest numbers are *before* those additional restrictions, which would only reduce trade count further.
6. **Funding costs are not modeled** (irrelevant for spot, would matter for perpetuals).

---

## 7. Reproduce

```bash
# Download data (public read-only endpoint, no credentials)
python scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 12000

# Run full validation (split + walk-forward + stress)
python scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy orb
python scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy vwap
```

Artifacts: `reports/backtest_{strategy}.json`, `reports/trades_{strategy}.csv`.

---

## 8. Conclusion

| Strategy | Sample | Verdict | Cleared for paper-forward? |
|---|---|---|---|
| ORB | 7 trades (insufficient) | **FRAGILE** | **No** |
| VWAP Trend Pullback | 98 trades | **FRAGILE — clearly losing** | **No** |

**Gate B (Backtest Validity) is NOT passed.** Neither strategy demonstrates a robust, cost-surviving edge. The backtest *infrastructure* is now correct and trustworthy — the strategies running on it are not yet viable.
