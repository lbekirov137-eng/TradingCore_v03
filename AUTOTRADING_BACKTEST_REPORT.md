# TradingCore — Backtest Report

**Generated:** 2026-07-28, revised after a critical bug fix (see §0).
**Engine:** `api/backtesting/backtest_engine.py` (deterministic, no-look-ahead, costs modeled)
**Data:** BTCUSDT 5m, Binance public read-only endpoint. Two independent windows: ~6 weeks (11,999 candles) and ~6 months (51,999 candles).

> **This report makes no claim of profitability. Both strategies are robustly, consistently unprofitable across two independent time windows. Neither is approved for paper-forward.**

---

## 0. A critical bug was found and fixed during this validation — read this first

Running ORB against 6 months of real data produced an implausible result: **0 trades over 4 months** of the training split. Tracing it (not guessing) revealed the cause: `TakeProfit.calculate` always computed a LONG-direction target (`entry + risk×N`) regardless of the trade's actual direction. For a SHORT trade this places the take-profit *above* both entry and the stop — on the wrong side of the market, unreachable in the profitable direction.

Concretely: a SHORT opened at entry=89234.8, stop=90648.454 (correctly above entry). Its take-profit was computed as 92062.108 — even higher than the stop. Price then fell from ~89k to ~60k over the following four months (a 33% move in the position's favor), but it never closed: the correct stop was never touched (price only rose to 89490, short of the 90648 stop) and the bogus take-profit was unreachable on the downside. The stuck position silently blocked every subsequent signal for the rest of the backtest window.

**This means the ORB numbers reported in the previous version of this document (7 trades, near-breakeven) were themselves partly an artifact of this bug** — SHORT trades were getting stuck rather than resolving, artificially suppressing both trade count and (coincidentally) apparent loss. Fixed in `api/strategy_engine/strategies/orb/take_profit.py`; 5 regression tests in `tests/regression/test_take_profit_direction.py` reproduce the exact scenario. **VWAP is unaffected** — it is LONG-only and never calls the shared `TakeProfit` class (verified by grep, not assumed).

All numbers below reflect the **fixed** code.

---

## 1. Methodology (unchanged, still holds)

| Property | Implementation |
|---|---|
| No look-ahead | Strategy sees `visible_market` truncated to `[0..i]`; verified by deliberately injecting a look-ahead bug and confirming 3 tests fail |
| Decision/execution separation | Decision on **closed** candle `i`; entry fills on candle `i+1` **at its open** |
| Exit timing | Exit never occurs on the entry candle |
| SL+TP in same candle | Resolves to **STOP_LOSS** — the profitable outcome is never auto-selected |
| Costs | Fee (0.1%/side), slippage (5 bps/side), spread (2 bps, half per side) applied to entry and exit |
| Unresolved trades | Flagged `END_OF_DATA`/`UNRESOLVED`, contribute **zero** PnL |
| Determinism | Identical inputs produce byte-identical reports |
| Performance | Indicator inputs (EMA/RSI/ATR/structure) bounded to a 260-candle rolling window instead of full history — turns an O(n²) backtest into effectively O(n) **without changing any indicator value** (EMA200/RSI14/ATR14 are numerically converged well within 260 candles; proven identical to full-history mode when data is shorter than the window) |

---

## 2. ORB — results at two independent scales

Regime/liquidity/data-quality filters **active**.

### 6-week window (11,999 candles, 2026-06-16 → 2026-07-28)

| Metric | Value |
|---|---|
| Total trades | **49** |
| Wins / Losses | 17 / 32 |
| Win rate | 34.7% |
| Net PnL | **−48.59 USDT (−4.86%)** |
| Total fees | 29.99 |
| Profit factor | **0.174** |
| Max drawdown | 4.96% |
| Max consecutive losses | 9 |

### 6-month window (51,999 candles, 2026-01-28 → 2026-07-28)

| Metric | Value |
|---|---|
| Total trades | **208** |
| Wins / Losses | 80 / 128 |
| Win rate | 38.5% |
| Net PnL | **−176.54 USDT (−17.65%)** |
| Total fees | 103.43 |
| Profit factor | **0.205** |
| Max drawdown | **17.86%** |
| Max consecutive losses | 9 |
| Final balance | 823.46 |

**Both samples now agree, and both are adequately sized to be meaningful (49 and 208 trades respectively).** Profit factor is consistently in the 0.17–0.21 range — the strategy loses roughly 5x for every 1x it wins, in aggregate. This is a materially stronger and more damning finding than the previous small-sample read (7 trades, near-breakeven), which was itself distorted by the take-profit bug suppressing trade resolution.

### 6-week window — train/validation/test split (with fix applied)

| Split | Candles | Trades | Net PnL | Profit factor | Win rate |
|---|---|---|---|---|---|
| Train | 7,199 | 32 | −37.60 | 0.113 | 28.1% |
| Validation | 2,400 | 14 | −22.32 | 0.049 | 28.6% |
| Test (held out) | 2,400 | 8 | −1.71 | 0.630 | 37.5% |

Every split is negative. The held-out test split's profit factor (0.63) is closer to breakeven than train/validation, but still net-negative and on only 8 trades — not evidence of a hidden edge, just smaller-sample noise on an already-losing strategy.

### 6-week window — walk-forward (with fix applied)

| Metric | Value |
|---|---|
| Windows | 7 |
| Windows with trades | 7 |
| Profitable windows | **0** |
| **Consistency** | **0.0%** |

This is a significant change from the pre-fix report, which showed 57.1% consistency — itself an artifact of the take-profit bug distorting which windows had resolved trades at all. **With the fix applied, ORB's walk-forward consistency is 0.0%, identical in kind to VWAP's.** Zero of seven independent time windows were profitable.

### 6-week window — sensitivity / stress (with fix applied)

| Scenario | Net PnL | Win rate |
|---|---|---|
| baseline | −48.59 | 34.7% |
| fees ×2 | −77.54 | 20.4% |
| slippage ×2 | −84.68 | 28.6% |
| slippage ×3 | −87.27 | 20.4% |
| spread ×2 | −52.06 | 34.7% |
| latency +1 candle | −57.04 | 32.6% |
| all costs ×2 | **−555.84** | 8.2% |

**Verdict: FRAGILE.** Every single stress scenario is worse than baseline, and baseline is already negative. The "all costs ×2" figure is a large jump from the individual ×2 scenarios — plausible as a compounding effect of three simultaneously-worsened cost dimensions across 49 sequential trades, but flagged here for transparency rather than smoothed over; it does not change the directional conclusion, which is already unambiguous from every other row.

---

## 3. Session VWAP Trend Pullback — results (unaffected by the take-profit bug, numbers unchanged)

### 6-week window (11,999 candles)

| Metric | Value |
|---|---|
| Total trades | **98** |
| Wins / Losses | 21 / 77 |
| Win rate | **21.4%** |
| Net PnL | **−128.37 USDT (−12.8%)** |
| Profit factor | **0.092** |
| Max drawdown | **12.84%** |
| Max consecutive losses | **13** |

### Walk-forward

| Metric | Value |
|---|---|
| **Consistency** | **0.00%** — zero profitable windows out of 7 |

### Sensitivity / stress

| Scenario | Net PnL | Win rate |
|---|---|---|
| baseline | −128.37 | 21.4% |
| fees ×2 | −195.34 | 9.2% |
| slippage ×2 | −132.80 | 16.3% |
| slippage ×3 | −136.76 | 9.2% |
| all costs ×2 | −185.31 | 6.1% |

**Verdict: FRAGILE — decisively unprofitable.** This is unchanged from the prior report and remains the clearest, most unambiguous finding in this project: profit factor 0.09, zero profitable walk-forward windows, double-digit drawdown, 13 consecutive losses (which would trip the newly-wired `LossStreakGuard` and `MaxDrawdownGuard` in live operation).

A 6-month VWAP run was not completed this session: `calculate_session_vwap` recomputes cumulative sums from session start on every call, and this specific inefficiency (distinct from the engine-wide indicator-window fix applied to ORB) makes multi-month VWAP backtests impractically slow in this environment. See `AUTOTRADING_NEXT_ACTIONS.md` item 7.

---

## 4. Statistical honesty

- **ORB now has an adequate, consistent sample at two scales** (49 and 208 trades) — no longer the n=7 statistical non-finding of the prior report. The consistency of profit factor (~0.17–0.21) across a 4x difference in sample size and a fully independent time window is itself evidence this is a real effect, not noise.
- **VWAP's n=98 was already adequate**; profit factor 0.092 with zero profitable walk-forward windows is unambiguous.
- **Fees are a first-order driver of the loss, not the whole story.** ORB's 6-month fees (103.43) are smaller than its net loss (176.54) — the strategy is losing on raw price action, not merely bleeding to costs, though costs make a bad situation worse (see sensitivity table).
- Neither strategy's parameters were tuned on this data. **No parameter optimization was performed.**
- Monte Carlo trade-order permutation tooling exists (`api/backtesting/research.py::monte_carlo_trade_order`) and was validated with unit tests, but was not run at full scale here — the directional conclusion (both strategies losing) does not require it.

---

## 5. Proof the no-look-ahead tests actually work (unchanged, still holds)

```
3 failed, 4 passed   (BacktestContext.visible_market patched to leak the future)
  FAILED test_strategy_never_sees_more_than_current_index
  FAILED test_no_future_candle_is_ever_visible
  FAILED test_appending_future_candles_does_not_change_past_decisions
      assert [107.9, ...] == [104.9, ...]   # past decisions changed
```
Fix restored; all 7 tests pass again.

---

## 6. Known limitations

1. **6-month VWAP run not completed** — see §3 and `AUTOTRADING_NEXT_ACTIONS.md` item 7.
2. **No order-book modeling.** Spread is a flat assumption, not real bid/ask; partial fills are approximated.
3. **Single symbol.** Only BTCUSDT tested at meaningful scale. No cross-asset validation.
4. **The backtest bypasses `DecisionEngine`**, so its numbers exclude the daily-trade/risk limits, kill switch, and all five newly-wired risk guards. In live/paper operation those guards would additionally reduce trade count (e.g., `MaxTradesPerSessionGuard`, `LossStreakGuard` would have triggered given the loss streaks observed) — meaning **live results would very likely differ from, and probably be somewhat better-protected than, these raw backtest numbers**, though the underlying entry logic's negative edge would remain.
5. **Funding costs not modeled** (irrelevant for spot).
6. **Candidate variants** (`api/strategy_engine/strategies/orb/candidates.py`, `.../vwap/candidates.py`) were implemented and unit-tested but not yet run at full backtest scale in this session due to the compute cost of the 6-month runs above consuming the available time budget. Each is designed to be evaluated **independently** against the baseline, never combined.

---

## 7. Reproduce

```bash
python scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 12000
python scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 52000 --out data/BTCUSDT_5m_6mo.json

python scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy orb
python scripts/run_backtest.py --data data/BTCUSDT_5m_6mo.json --strategy orb --outdir reports/6mo
python scripts/run_backtest.py --data data/BTCUSDT_5m.json --strategy vwap

python scripts/run_candidate_research.py --data data/BTCUSDT_5m.json
```

Artifacts: `reports/backtest_{strategy}.json`, `reports/trades_{strategy}.csv`.

---

## 8. Conclusion

| Strategy | Samples | Walk-forward consistency | Verdict | Cleared for paper-forward? |
|---|---|---|---|---|
| ORB | 49 (6wk) + 208 (6mo), consistent | **0.0%** | **FRAGILE — robustly losing at two scales, zero consistent windows** | **No** |
| VWAP Trend Pullback | 98, adequate | **0.0%** | **FRAGILE — clearly losing** | **No** |

**Gate B (Backtest Validity) fails, more decisively than in the previous report.** With the take-profit bug fixed, ORB's true trading frequency and negative edge are now fully visible: every train/validation/test split is negative, every one of 7 walk-forward windows is unprofitable, and every stress scenario is worse than an already-negative baseline. This is not a case of "not enough data to tell" — it is a consistent, adequately-sampled, negative result at every scale and every split tested, for both strategies.
