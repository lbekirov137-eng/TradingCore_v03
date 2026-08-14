#!/usr/bin/env python3
"""
TradingCore Frequent Edge Research Lab
======================================

RESEARCH ONLY. No exchange order client, no API keys, no champion mutation,
no LIVE path.

Purpose: search for a more frequent LONG-only edge without tuning the already
frozen BTC 1H challenger. Three distinct mean-reversion hypotheses are fixed in
this file before the final holdout is opened. Candidate selection uses only the
early development sample. Exactly one global winner is then evaluated on its
untouched final 30% history with the repository promotion gates.

Hypotheses (fixed, not optimized at runtime):
1) VWAP overshoot -> bullish reclaim while still below session VWAP.
2) ATR flush -> bullish reversal after an outsized one-bar drop.
3) EMA20 lower-band re-entry in a non-trending regime.

All strategies:
- closed candles only;
- SPOT LONG only;
- $1 risk (0.1% of $1,000) via existing backtest runner;
- stop distance must imply <=1x spot notional;
- costs/slippage checked before entry and charged again by the backtest engine;
- final holdout never participates in candidate selection.
"""

from __future__ import annotations

import argparse
import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import strategy_lab_orchestrator as broad

from api.strategy_engine.backtest_runner import run_backtest
from api.strategy_engine.cost_gate import evaluate_cost_viability
from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    CandleWindow,
    StrategyConfig,
    StrategyDecision,
    atr,
    ema,
    session_vwap,
)
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import build_stats
from api.strategy_supervisor.validation import (
    build_walk_forward_windows,
    robustness_ratio,
)


SCHEMA = "TRADINGCORE_FREQUENT_EDGE_RESEARCH_V1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "LTCUSDT", "SOLUSDT", "BCHUSDT")
TIMEFRAMES = {
    "30m": {"ms": 1_800_000, "max_bars": 48},
    "1h": {"ms": 3_600_000, "max_bars": 24},
}
DEV_FRACTION_DEFAULT = 0.70
RISK_AMOUNT_USD = 1.0
CAPITAL_USD = 1000.0
MIN_RR = 2.0


def patch_intervals() -> None:
    broad.INTERVAL_MS.update({"30m": TIMEFRAMES["30m"]["ms"]})
    from api.strategy_engine import dataset_quality
    dataset_quality.INTERVAL_MS.update({"30m": TIMEFRAMES["30m"]["ms"]})


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _session_slice(window: CandleWindow) -> list:
    current_day = datetime.fromtimestamp(
        window.current.open_time_ms / 1000.0, tz=timezone.utc
    ).date()
    rows = []
    for offset in range(len(window)):
        candle = window[-1 - offset]
        day = datetime.fromtimestamp(
            candle.open_time_ms / 1000.0, tz=timezone.utc
        ).date()
        if day != current_day:
            break
        rows.append(candle)
    rows.reverse()
    return rows


class ResearchMeanReversionBase(BaseStrategy):
    version = "1.0.0-research-frozen"

    def _common(self, window: CandleWindow) -> dict[str, Any] | None:
        history = window.slice(self.config.warmup_bars)
        atr_value = atr(history, self.config.atr_period)
        if atr_value is None or atr_value <= 0:
            return None
        closes = window.closes(self.config.warmup_bars)
        fast = ema(closes, 20)
        slow = ema(closes, 50)
        if fast is None or slow is None:
            return None
        gap_pct = abs(fast - slow) / window.current.close * 100.0
        atr_pct = atr_value / window.current.close * 100.0
        return {
            "history": history,
            "atr": atr_value,
            "fast": fast,
            "slow": slow,
            "gap_pct": gap_pct,
            "atr_pct": atr_pct,
        }

    def _finalise(
        self,
        *,
        entry: float,
        stop: float,
        target: float,
        reason: str,
        diagnostics: dict[str, Any],
    ) -> StrategyDecision:
        if not (stop > 0 and stop < entry < target):
            return self.no_trade("INVALID_GEOMETRY", **diagnostics)

        risk_per_unit = entry - stop
        rr = (target - entry) / risk_per_unit

        if rr < MIN_RR:
            return self.no_trade(
                "RISK_REWARD_BELOW_MINIMUM",
                risk_reward=round(rr, 4),
                minimum=MIN_RR,
                **diagnostics,
            )

        # $1 / stop_distance is the runner quantity. Requiring notional <= $1000
        # makes the research geometry consistent with 1x spot, no borrowing.
        position_notional = (RISK_AMOUNT_USD / risk_per_unit) * entry
        if position_notional > CAPITAL_USD + 1e-9:
            return self.no_trade(
                "STOP_TOO_TIGHT_FOR_1X_SPOT",
                position_notional=round(position_notional, 2),
                **diagnostics,
            )

        viability = evaluate_cost_viability(
            entry=entry,
            stop=stop,
            take_profit=target,
            risk_amount=RISK_AMOUNT_USD,
        )
        if not viability.get("viable"):
            return self.no_trade(
                viability.get("reason_code", "COST_GATE_REJECTED"),
                estimated_cost_r=viability.get("estimated_cost_r"),
                net_rr_after_costs=viability.get("net_rr_after_costs"),
                **diagnostics,
            )

        # Supported research symbols all use $0.01 tick in the project fixture.
        px_entry = round(float(entry), 2)
        px_stop = round(float(stop), 2)
        px_target = round(float(target), 2)
        px_tp1 = round(px_entry + (px_entry - px_stop), 2)

        if not (px_stop < px_entry < px_tp1 < px_target):
            return self.no_trade("ROUNDING_COLLAPSED_GEOMETRY", **diagnostics)

        realised_rr = (px_target - px_entry) / (px_entry - px_stop)
        if realised_rr < MIN_RR:
            return self.no_trade("ROUNDING_RR_BELOW_MINIMUM", **diagnostics)

        return StrategyDecision(
            strategy_key=self.strategy_key,
            version=self.version,
            signal="BUY",
            reason_code=reason,
            entry=px_entry,
            stop=px_stop,
            take_profit_1=px_tp1,
            take_profit_2=px_target,
            risk_reward=round(realised_rr, 4),
            diagnostics={
                **diagnostics,
                "estimated_cost_r": viability.get("estimated_cost_r"),
                "net_rr_after_costs": viability.get("net_rr_after_costs"),
                "position_notional": round(position_notional, 2),
                "risk_amount": RISK_AMOUNT_USD,
                "leverage": 1,
            },
        )


class VwapOvershootReclaim(ResearchMeanReversionBase):
    strategy_key = "MR_VWAP_OVERSHOOT_RECLAIM"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 3:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        # Mean reversion only when trend is not strong.
        if common["gap_pct"] > 0.80:
            return self.no_trade("TREND_TOO_STRONG", gap_pct=round(common["gap_pct"], 4))

        session = _session_slice(window)
        if len(session) < 4:
            return self.no_trade("SESSION_TOO_SHORT")

        anchor = session_vwap(session[:-1])
        if anchor is None:
            return self.no_trade("VWAP_UNAVAILABLE")

        previous = window[-2]
        current = window.current
        atr_value = common["atr"]

        if previous.close >= anchor - 0.60 * atr_value:
            return self.no_trade("NO_VWAP_OVERSHOOT")
        if not (current.close > current.open and current.close > previous.close):
            return self.no_trade("NO_BULLISH_RECLAIM")
        if current.close >= anchor:
            return self.no_trade("REVERSION_ALREADY_COMPLETE")

        recent_lows = [window[-1].low, window[-2].low, window[-3].low]
        stop = min(recent_lows) - 0.10 * atr_value

        return self._finalise(
            entry=current.close,
            stop=stop,
            target=anchor,
            reason="VWAP_OVERSHOOT_RECLAIM_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "session_vwap": round(anchor, 4),
            },
        )


class AtrFlushReversal(ResearchMeanReversionBase):
    strategy_key = "MR_ATR_FLUSH_REVERSAL"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 4:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        if common["gap_pct"] > 1.00:
            return self.no_trade("TREND_TOO_STRONG", gap_pct=round(common["gap_pct"], 4))

        before = window[-3]
        previous = window[-2]
        current = window.current
        atr_value = common["atr"]

        drop_atr = (before.close - previous.close) / atr_value
        if drop_atr < 1.25:
            return self.no_trade("FLUSH_TOO_SMALL", drop_atr=round(drop_atr, 4))

        previous_mid = (previous.high + previous.low) / 2.0
        if not (current.close > current.open and current.close > previous_mid):
            return self.no_trade("REVERSAL_NOT_CONFIRMED")

        stop = min(previous.low, current.low) - 0.15 * atr_value
        risk = current.close - stop
        if risk <= 0:
            return self.no_trade("INVALID_RISK")
        target = current.close + 2.50 * risk

        return self._finalise(
            entry=current.close,
            stop=stop,
            target=target,
            reason="ATR_FLUSH_REVERSAL_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "drop_atr": round(drop_atr, 4),
            },
        )


class EmaBandReentry(ResearchMeanReversionBase):
    strategy_key = "MR_EMA20_BAND_REENTRY"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < self.config.warmup_bars + 1:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        closes_now = window.closes(self.config.warmup_bars)
        closes_prev = window.slice(self.config.warmup_bars + 1)[:-1]
        ema_now = ema(closes_now, 20)
        ema_prev = ema([c.close for c in closes_prev], 20)
        if ema_now is None or ema_prev is None:
            return self.no_trade("EMA_UNAVAILABLE")

        current = window.current
        previous = window[-2]
        atr_value = common["atr"]
        slope_pct = abs(ema_now - ema_prev) / current.close * 100.0

        if common["gap_pct"] > 0.80 or slope_pct > 0.20:
            return self.no_trade("REGIME_NOT_FLAT")

        if previous.close >= ema_prev - 1.00 * atr_value:
            return self.no_trade("NO_LOWER_BAND_OVERSHOOT")
        if not (current.close > current.open and current.close > ema_now - 0.50 * atr_value):
            return self.no_trade("NO_BAND_REENTRY")
        if current.close >= ema_now:
            return self.no_trade("REVERSION_ALREADY_COMPLETE")

        stop = min(previous.low, current.low) - 0.10 * atr_value

        return self._finalise(
            entry=current.close,
            stop=stop,
            target=ema_now,
            reason="EMA20_BAND_REENTRY_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "ema20_slope_percent": round(slope_pct, 4),
                "ema20": round(ema_now, 4),
            },
        )


HYPOTHESES = (VwapOvershootReclaim, AtrFlushReversal, EmaBandReentry)


def score(stats: dict[str, Any], robust: float | None, wf: bool) -> tuple:
    def n(value: Any, fallback: float = -1e9) -> float:
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else fallback

    trades = stats.get("closed_trades") or 0
    pf = n(stats.get("profit_factor"))
    exp = n(stats.get("expectancy_r"))
    pnl = n(stats.get("net_pnl"))
    dd = n(stats.get("max_drawdown_r"), 1e9)

    adequate = 1 if trades >= 40 else 0
    economically_positive = 1 if pf >= 1.10 and exp > 0 and pnl > 0 else 0
    robust_ok = 1 if isinstance(robust, (int, float)) and robust >= 0.50 and wf else 0

    return (
        adequate,
        economically_positive,
        robust_ok,
        n(robust),
        pf,
        exp,
        pnl,
        -dd,
        trades,
    )


def suppress_before(strategy: BaseStrategy, boundary_ms: int) -> None:
    original = strategy.evaluate_closed_candle

    def gated(self, candles, index):
        if int(candles[index].open_time_ms) < boundary_ms:
            return self.no_trade("PRE_FINAL_HOLDOUT")
        return original(candles, index)

    strategy.evaluate_closed_candle = types.MethodType(gated, strategy)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3000)
    parser.add_argument("--dev-fraction", type=float, default=DEV_FRACTION_DEFAULT)
    parser.add_argument("--output", default="frequent_edge_results")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if not 0.60 <= args.dev_fraction <= 0.80:
        raise SystemExit("--dev-fraction must be between 0.60 and 0.80")

    from config.startup_safety import assert_safe_startup
    safety = assert_safe_startup()
    patch_intervals()

    out = Path(args.output)
    if not out.is_absolute():
        out = Path.cwd() / out
    data_dir = out / "datasets"
    out.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    from api.strategy_engine.dataset_quality import audit_dataset, load_research_candles, quality_verdict

    print("=" * 96)
    print("TRADINGCORE FREQUENT EDGE RESEARCH — STRICT GLOBAL HOLDOUT")
    print("=" * 96)
    print("Safety:", safety)
    print("Symbols:", ", ".join(SYMBOLS))
    print("Timeframes:", ", ".join(TIMEFRAMES))
    print("Hypotheses:", ", ".join(cls.strategy_key for cls in HYPOTHESES))
    print("Selection: DEVELOPMENT ONLY; exactly one global candidate opens final holdout")
    print("REAL ORDERS: DISABLED / NO ORDER CODE")
    print("=" * 96, flush=True)

    datasets: dict[tuple[str, str], list[Any]] = {}
    boundaries: dict[tuple[str, str], int] = {}
    audits: dict[str, Any] = {}

    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            path = data_dir / f"{symbol}_{timeframe}_{args.days}d.json"
            dl = broad.download_dataset(symbol, timeframe, args.days, path, args.refresh)
            report = audit_dataset(path)
            verdict = quality_verdict(report)
            audits[f"{symbol}:{timeframe}"] = {"download": dl, "report": report, "verdict": verdict}
            if not verdict.get("usable"):
                print(f"[DATA] {symbol} {timeframe}: unusable {verdict.get('reasons')}", flush=True)
                continue
            rows = load_research_candles(path)
            if len(rows) < 500:
                continue
            split_index = max(1, min(len(rows) - 1, int(len(rows) * args.dev_fraction)))
            datasets[(symbol, timeframe)] = rows
            boundaries[(symbol, timeframe)] = int(rows[split_index].open_time_ms)
            print(
                f"[DATA] {symbol} {timeframe}: candles={len(rows)} "
                f"dev_end={_utc(boundaries[(symbol, timeframe)])}",
                flush=True,
            )

    development: list[dict[str, Any]] = []

    for (symbol, timeframe), rows in datasets.items():
        boundary = boundaries[(symbol, timeframe)]
        dev_rows = [c for c in rows if int(c.open_time_ms) < boundary]
        for cls in HYPOTHESES:
            strategy = cls(StrategyConfig(warmup_bars=60))
            strategy.strategy_key = f"{cls.strategy_key}_{timeframe.upper()}"
            bt = run_backtest(
                strategy,
                dev_rows,
                max_bars_in_trade=TIMEFRAMES[timeframe]["max_bars"],
            )
            stats = build_stats(bt["trades"])
            windows = build_walk_forward_windows(bt["trades"], window_count=4)
            robust = robustness_ratio(windows)
            wf = bool(windows and robust is not None and robust > 0.0)
            item = {
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy_class": cls.__name__,
                "strategy_key": strategy.strategy_key,
                "boundary_ms": boundary,
                "boundary_utc": _utc(boundary),
                "stats": stats,
                "robustness_ratio": robust,
                "walk_forward_passed": wf,
                "signals": bt["signals"],
                "score": score(stats, robust, wf),
            }
            development.append(item)
            print(
                f"[DEV] {symbol} {timeframe} {cls.strategy_key}: "
                f"trades={stats['closed_trades']} PF={stats['profit_factor']} "
                f"expR={stats['expectancy_r']} net={stats['net_pnl']} "
                f"DD_R={stats['max_drawdown_r']} robust={robust} WF={wf}",
                flush=True,
            )

    if not development:
        raise SystemExit("No development candidates")

    chosen = max(development, key=lambda item: item["score"])
    symbol = chosen["symbol"]
    timeframe = chosen["timeframe"]
    boundary = chosen["boundary_ms"]
    boundary_utc = chosen["boundary_utc"]
    cls = next(item for item in HYPOTHESES if item.__name__ == chosen["strategy_class"])

    print(
        f"[FROZEN GLOBAL CHOICE] {symbol} {timeframe} {chosen['strategy_key']} "
        f"— final holdout not used for selection",
        flush=True,
    )

    strategy = cls(StrategyConfig(warmup_bars=60))
    strategy.strategy_key = chosen["strategy_key"]
    suppress_before(strategy, boundary)

    final_bt = run_backtest(
        strategy,
        datasets[(symbol, timeframe)],
        max_bars_in_trade=TIMEFRAMES[timeframe]["max_bars"],
    )
    final_trades = [
        trade for trade in final_bt["trades"]
        if isinstance(trade.closed_at_utc, str) and trade.closed_at_utc >= boundary_utc
    ]
    final_stats = build_stats(final_trades)

    validation = {
        "strategy_id": chosen["strategy_key"],
        "sample_id": f"FREQUENT_EDGE_STRICT_{symbol}_{timeframe}_{boundary}",
        "oos_trades": final_stats["closed_trades"],
        "oos_net_pnl": final_stats["net_pnl"],
        "oos_profit_factor": final_stats["profit_factor"],
        "oos_expectancy_r": final_stats["expectancy_r"],
        "oos_max_drawdown_r": final_stats["max_drawdown_r"],
        "oos_win_rate_percent": final_stats["win_rate_percent"],
        "robustness_ratio": chosen["robustness_ratio"],
        "walk_forward_passed": chosen["walk_forward_passed"],
        "look_ahead_leakage": False,
        "safety_violations": [],
        "holdout_start_utc": boundary_utc,
        "note": (
            "Three hypotheses x two timeframes x six high-liquidity symbols were compared "
            "on development data only. Exactly one global winner was frozen before its "
            "final 30% holdout was opened. Costs/slippage included."
        ),
    }
    gates = promotion_gates(validation)

    ranking = sorted(development, key=lambda item: item["score"], reverse=True)
    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "RESEARCH_ONLY",
        "real_orders_enabled": False,
        "paper_champion_modified": False,
        "days": args.days,
        "development_fraction": args.dev_fraction,
        "symbols": list(SYMBOLS),
        "timeframes": list(TIMEFRAMES),
        "hypotheses": [cls.strategy_key for cls in HYPOTHESES],
        "development_ranking": ranking,
        "frozen_choice": chosen,
        "final_untouched_holdout": {
            "stats": final_stats,
            "validation": validation,
            "promotion_gates": gates,
        },
        "dataset_audits": audits,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"frequent_edge_{stamp}.json"
    text_path = out / f"frequent_edge_{stamp}.txt"
    latest_path = out / "LATEST_FREQUENT_EDGE.txt"

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)

    lines = [
        "=" * 96,
        "TRADINGCORE FREQUENT EDGE RESEARCH — STRICT GLOBAL HOLDOUT",
        "=" * 96,
        f"Generated UTC: {report['generated_at_utc']}",
        f"Frozen choice: {symbol} {timeframe} {chosen['strategy_key']}",
        f"Holdout start: {boundary_utc}",
        "",
        "TOP DEVELOPMENT CANDIDATES:",
    ]
    for index, item in enumerate(ranking[:10], start=1):
        s = item["stats"]
        lines.append(
            f"{index:02d}. {item['symbol']} {item['timeframe']} {item['strategy_key']} "
            f"trades={s['closed_trades']} PF={s['profit_factor']} expR={s['expectancy_r']} "
            f"net={s['net_pnl']} DD_R={s['max_drawdown_r']} "
            f"robust={item['robustness_ratio']} WF={item['walk_forward_passed']}"
        )

    lines.extend([
        "",
        "FINAL UNTOUCHED HOLDOUT:",
        f"  trades={final_stats['closed_trades']} PF={final_stats['profit_factor']} "
        f"expR={final_stats['expectancy_r']} net={final_stats['net_pnl']} "
        f"DD_R={final_stats['max_drawdown_r']} win={final_stats['win_rate_percent']}",
        f"  promotion_passed={gates['passed']}",
        f"  failed_gates={','.join(gates['failed_gates']) if gates['failed_gates'] else 'NONE'}",
        "",
        "RESEARCH ONLY. NO LIVE ORDER PATH. A pass still requires forward PAPER confirmation.",
    ])

    text = "\n".join(lines)
    text_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")

    print("\n" + text)
    print("\nJSON:", json_path)
    print("TEXT:", text_path)
    print("LATEST:", latest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
