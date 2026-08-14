#!/usr/bin/env python3
"""
TradingCore Regime + Liquidity Reversal Research Lab
====================================================

RESEARCH ONLY. No exchange order client, no API keys, no champion mutation,
no LIVE path.

This is a NEW research family. It does not tune or reuse the failed SOL/BCH
holdout outcomes from frequent_edge_research.py.

Protocol frozen in code before final holdout inspection:
- symbols: BTC, ETH, BNB, SOL, LTC, BCH spot USDT pairs;
- execution timeframes: 15m, 30m, 1h;
- three distinct LONG-only reversal hypotheses;
- first 70% of each symbol/timeframe is DEVELOPMENT;
- candidates must pass a strict development eligibility screen;
- exactly ONE global development winner is frozen;
- only that winner may open its final 30% untouched holdout;
- repository promotion_gates remain authoritative.

The hypotheses add regime/liquidity structure rather than changing previously
observed VWAP parameters after seeing their holdouts.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import strategy_lab_orchestrator as broad

from api.strategy_engine.backtest_runner import run_backtest
from api.strategy_engine.strategies.contracts import (
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
from config.startup_safety import assert_safe_startup
from frequent_edge_research import ResearchMeanReversionBase


SCHEMA = "TRADINGCORE_REGIME_LIQUIDITY_REVERSAL_V1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "LTCUSDT", "SOLUSDT", "BCHUSDT")
TIMEFRAMES = {
    "15m": {"ms": 900_000, "max_bars": 96},
    "30m": {"ms": 1_800_000, "max_bars": 48},
    "1h": {"ms": 3_600_000, "max_bars": 24},
}
DAYS_DEFAULT = 1500
DEV_FRACTION_DEFAULT = 0.70


def patch_intervals() -> None:
    broad.INTERVAL_MS.update({
        "15m": TIMEFRAMES["15m"]["ms"],
        "30m": TIMEFRAMES["30m"]["ms"],
    })
    from api.strategy_engine import dataset_quality
    dataset_quality.INTERVAL_MS.update({
        "15m": TIMEFRAMES["15m"]["ms"],
        "30m": TIMEFRAMES["30m"]["ms"],
    })


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _session_slice(window: CandleWindow) -> list:
    day = datetime.fromtimestamp(
        window.current.open_time_ms / 1000.0, tz=timezone.utc
    ).date()
    rows = []
    for offset in range(len(window)):
        candle = window[-1 - offset]
        candle_day = datetime.fromtimestamp(
            candle.open_time_ms / 1000.0, tz=timezone.utc
        ).date()
        if candle_day != day:
            break
        rows.append(candle)
    rows.reverse()
    return rows


def _relative_volume(window: CandleWindow, lookback: int = 20) -> float | None:
    if len(window) < lookback + 1:
        return None
    prior = [float(window[-1 - offset].volume) for offset in range(1, lookback + 1)]
    baseline = statistics.median(prior)
    if baseline <= 0:
        return None
    return float(window.current.volume) / baseline


def _previous_relative_volume(window: CandleWindow, lookback: int = 20) -> float | None:
    if len(window) < lookback + 2:
        return None
    prior = [float(window[-2 - offset].volume) for offset in range(1, lookback + 1)]
    baseline = statistics.median(prior)
    if baseline <= 0:
        return None
    return float(window[-2].volume) / baseline


def _utc_hour(window: CandleWindow) -> int:
    return datetime.fromtimestamp(
        window.current.open_time_ms / 1000.0, tz=timezone.utc
    ).hour


class SessionLiquidityVwapReclaim(ResearchMeanReversionBase):
    """VWAP overshoot reclaim only in liquid global-session hours."""

    strategy_key = "RL_SESSION_LIQUID_VWAP_RECLAIM"
    version = "1.0.0-research-frozen"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 22:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        # Fixed liquid-session proxy: European morning through NY afternoon.
        hour = _utc_hour(window)
        if not 7 <= hour < 19:
            return self.no_trade("OUTSIDE_LIQUID_SESSION", utc_hour=hour)

        if common["gap_pct"] > 0.60:
            return self.no_trade("TREND_TOO_STRONG", gap_pct=round(common["gap_pct"], 4))

        rel_volume = _relative_volume(window)
        if rel_volume is None or rel_volume < 0.90 or rel_volume > 3.00:
            return self.no_trade(
                "LIQUIDITY_FILTER_REJECTED",
                relative_volume=round(rel_volume, 4) if rel_volume is not None else None,
            )

        session = _session_slice(window)
        if len(session) < 4:
            return self.no_trade("SESSION_TOO_SHORT")

        anchor = session_vwap(session[:-1])
        if anchor is None:
            return self.no_trade("VWAP_UNAVAILABLE")

        previous = window[-2]
        current = window.current
        atr_value = common["atr"]

        if previous.close >= anchor - 0.50 * atr_value:
            return self.no_trade("NO_VWAP_OVERSHOOT")

        if not (
            current.close > current.open
            and current.close > previous.close
            and current.close > previous.high
        ):
            return self.no_trade("NO_CONFIRMED_RECLAIM")

        if current.close >= anchor:
            return self.no_trade("REVERSION_ALREADY_COMPLETE")

        stop = min(window[-1].low, window[-2].low, window[-3].low) - 0.10 * atr_value

        return self._finalise(
            entry=current.close,
            stop=stop,
            target=anchor,
            reason="SESSION_LIQUID_VWAP_RECLAIM_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "relative_volume": round(rel_volume, 4),
                "utc_hour": hour,
                "session_vwap": round(anchor, 4),
            },
        )


class VolumeFlushMidpointReclaim(ResearchMeanReversionBase):
    """High-volume downside flush followed by bullish midpoint reclaim."""

    strategy_key = "RL_VOLUME_FLUSH_MIDPOINT_RECLAIM"
    version = "1.0.0-research-frozen"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 24:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        if common["gap_pct"] > 0.80:
            return self.no_trade("TREND_TOO_STRONG", gap_pct=round(common["gap_pct"], 4))

        previous = window[-2]
        current = window.current
        atr_value = common["atr"]

        previous_range = previous.high - previous.low
        previous_body = abs(previous.close - previous.open)
        previous_return = previous.close - previous.open

        if not (
            previous_return < 0
            and previous_range >= 1.00 * atr_value
            and previous_body >= 0.55 * atr_value
        ):
            return self.no_trade("NO_DOWNSIDE_FLUSH")

        prior_rel_volume = _previous_relative_volume(window)
        if prior_rel_volume is None or prior_rel_volume < 1.25:
            return self.no_trade(
                "FLUSH_VOLUME_TOO_LOW",
                previous_relative_volume=(
                    round(prior_rel_volume, 4) if prior_rel_volume is not None else None
                ),
            )

        midpoint = (previous.high + previous.low) / 2.0
        if not (current.close > current.open and current.close > midpoint):
            return self.no_trade("NO_MIDPOINT_RECLAIM")

        stop = min(previous.low, current.low) - 0.10 * atr_value
        target = common["fast"]

        if target <= current.close:
            return self.no_trade("EMA20_TARGET_NOT_ABOVE_ENTRY")

        return self._finalise(
            entry=current.close,
            stop=stop,
            target=target,
            reason="VOLUME_FLUSH_MIDPOINT_RECLAIM_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "previous_relative_volume": round(prior_rel_volume, 4),
                "flush_range_atr": round(previous_range / atr_value, 4),
                "ema20_target": round(target, 4),
            },
        )


class RangeLowerBandReentry(ResearchMeanReversionBase):
    """Range-regime lower EMA/ATR band excursion followed by re-entry."""

    strategy_key = "RL_RANGE_LOWER_BAND_REENTRY"
    version = "1.0.0-research-frozen"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 22:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        if common["gap_pct"] > 0.45:
            return self.no_trade("NOT_RANGE", gap_pct=round(common["gap_pct"], 4))

        if not 0.12 <= common["atr_pct"] <= 1.20:
            return self.no_trade("VOLATILITY_OUTSIDE_RANGE_BAND", atr_pct=round(common["atr_pct"], 4))

        previous = window[-2]
        current = window.current
        atr_value = common["atr"]
        lower_band = common["fast"] - 0.80 * atr_value
        reentry_band = common["fast"] - 0.45 * atr_value

        if previous.close > lower_band and previous.low > lower_band:
            return self.no_trade("NO_LOWER_BAND_EXCURSION")

        rel_volume = _relative_volume(window)
        if rel_volume is None or rel_volume < 0.80:
            return self.no_trade(
                "REENTRY_VOLUME_TOO_LOW",
                relative_volume=round(rel_volume, 4) if rel_volume is not None else None,
            )

        if not (
            current.close > current.open
            and current.close > reentry_band
            and current.close > previous.close
        ):
            return self.no_trade("NO_BULLISH_BAND_REENTRY")

        stop = min(previous.low, current.low) - 0.10 * atr_value
        target = common["fast"] + 0.30 * atr_value

        return self._finalise(
            entry=current.close,
            stop=stop,
            target=target,
            reason="RANGE_LOWER_BAND_REENTRY_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "relative_volume": round(rel_volume, 4),
                "lower_band": round(lower_band, 4),
                "reentry_band": round(reentry_band, 4),
            },
        )


HYPOTHESES = (
    SessionLiquidityVwapReclaim,
    VolumeFlushMidpointReclaim,
    RangeLowerBandReentry,
)


def development_eligible(stats: dict[str, Any], robust: float | None, wf: bool) -> bool:
    pf = stats.get("profit_factor")
    exp = stats.get("expectancy_r")
    net = stats.get("net_pnl")
    dd = stats.get("max_drawdown_r")
    trades = stats.get("closed_trades") or 0

    numeric = all(isinstance(value, (int, float)) for value in (pf, exp, net, dd))
    if not numeric:
        return False

    return bool(
        trades >= 50
        and float(pf) >= 1.20
        and float(exp) > 0
        and float(net) > 0
        and float(dd) <= 10.0
        and isinstance(robust, (int, float))
        and float(robust) >= 0.75
        and wf
    )


def score(item: dict[str, Any]) -> tuple:
    stats = item["stats"]
    def n(value: Any, fallback: float = -1e9) -> float:
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else fallback

    return (
        1 if item["development_eligible"] else 0,
        n(item["robustness_ratio"]),
        n(stats.get("profit_factor")),
        n(stats.get("expectancy_r")),
        n(stats.get("net_pnl")),
        -n(stats.get("max_drawdown_r"), fallback=1e9),
        stats.get("closed_trades") or 0,
    )


def suppress_before(strategy: Any, boundary_ms: int) -> None:
    original = strategy.evaluate_closed_candle

    def gated(self, candles, index):
        if int(candles[index].open_time_ms) < boundary_ms:
            return self.no_trade("PRE_FINAL_HOLDOUT")
        return original(candles, index)

    strategy.evaluate_closed_candle = types.MethodType(gated, strategy)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DAYS_DEFAULT)
    parser.add_argument("--dev-fraction", type=float, default=DEV_FRACTION_DEFAULT)
    parser.add_argument("--download-workers", type=int, default=3)
    parser.add_argument("--output", default="regime_liquidity_results")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if args.days < 730:
        raise SystemExit("--days must be >= 730")
    if not 0.60 <= args.dev_fraction <= 0.80:
        raise SystemExit("--dev-fraction must be between 0.60 and 0.80")

    safety = assert_safe_startup()
    patch_intervals()

    out = Path(args.output)
    if not out.is_absolute():
        out = Path.cwd() / out
    data_dir = out / "datasets"
    out.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("TRADINGCORE REGIME + LIQUIDITY REVERSAL RESEARCH — STRICT GLOBAL HOLDOUT")
    print("=" * 100)
    print("Safety:", safety)
    print("Symbols:", ", ".join(SYMBOLS))
    print("Timeframes:", ", ".join(TIMEFRAMES))
    print("Hypotheses:", ", ".join(cls.strategy_key for cls in HYPOTHESES))
    print("Development eligibility: trades>=50 PF>=1.20 expR>0 net>0 DD<=10R robust>=0.75 WF=True")
    print("Selection: DEVELOPMENT ONLY; exactly one global candidate may open holdout")
    print("REAL ORDERS: DISABLED / NO ORDER CODE")
    print("=" * 100, flush=True)

    requests_to_make = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            path = data_dir / f"{symbol}_{timeframe}_{args.days}d.json"
            requests_to_make.append((symbol, timeframe, path))

    downloads: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.download_workers)) as pool:
        future_map = {
            pool.submit(
                broad.download_dataset,
                symbol,
                timeframe,
                args.days,
                path,
                args.refresh,
            ): (symbol, timeframe, path)
            for symbol, timeframe, path in requests_to_make
        }
        for future in as_completed(future_map):
            symbol, timeframe, path = future_map[future]
            try:
                result = future.result()
                downloads.append(result)
                print(
                    f"[DATA] {symbol} {timeframe}: rows={result['rows']} cached={result['cached']}",
                    flush=True,
                )
            except Exception as error:
                downloads.append({
                    "symbol": symbol,
                    "interval": timeframe,
                    "path": str(path),
                    "error": f"{type(error).__name__}: {error}",
                })
                print(f"[DATA] {symbol} {timeframe}: FAILED {error}", flush=True)

    from api.strategy_engine.dataset_quality import (
        audit_dataset,
        load_research_candles,
        quality_verdict,
    )

    datasets: dict[tuple[str, str], list[Any]] = {}
    audits: dict[str, Any] = {}
    boundaries: dict[tuple[str, str], int] = {}

    for item in downloads:
        if item.get("error"):
            continue
        symbol = item["symbol"]
        timeframe = item["interval"]
        path = Path(item["path"])
        report = audit_dataset(path)
        verdict = quality_verdict(report)
        audits[f"{symbol}:{timeframe}"] = {"report": report, "verdict": verdict}
        if not verdict.get("usable"):
            continue
        rows = load_research_candles(path)
        if len(rows) < 1000:
            continue
        split_index = max(1, min(len(rows) - 1, int(len(rows) * args.dev_fraction)))
        boundary = int(rows[split_index].open_time_ms)
        datasets[(symbol, timeframe)] = rows
        boundaries[(symbol, timeframe)] = boundary

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
            eligible = development_eligible(stats, robust, wf)

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
                "development_eligible": eligible,
                "signals": bt["signals"],
            }
            development.append(item)
            print(
                f"[DEV] {symbol} {timeframe} {strategy.strategy_key}: "
                f"trades={stats['closed_trades']} PF={stats['profit_factor']} "
                f"expR={stats['expectancy_r']} net={stats['net_pnl']} "
                f"DD_R={stats['max_drawdown_r']} robust={robust} WF={wf} eligible={eligible}",
                flush=True,
            )

    ranking = sorted(development, key=score, reverse=True)
    eligible = [item for item in ranking if item["development_eligible"]]

    report: dict[str, Any] = {
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
        "eligible_count": len(eligible),
        "final_untouched_holdout": None,
    }

    final_text_lines: list[str] = []

    if eligible:
        chosen = eligible[0]
        symbol = chosen["symbol"]
        timeframe = chosen["timeframe"]
        boundary = chosen["boundary_ms"]
        boundary_utc = chosen["boundary_utc"]
        cls = next(item for item in HYPOTHESES if item.__name__ == chosen["strategy_class"])

        print(
            f"[FROZEN GLOBAL CHOICE] {symbol} {timeframe} {chosen['strategy_key']} "
            "— final holdout not used for selection",
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
            "sample_id": f"REGIME_LIQUIDITY_STRICT_{symbol}_{timeframe}_{boundary}",
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
                "New regime/liquidity research family. Three frozen hypotheses x three "
                "timeframes x six symbols were compared on development data only. "
                "Exactly one eligible winner opened its final 30% holdout."
            ),
        }
        gates = promotion_gates(validation)

        report["frozen_choice"] = chosen
        report["final_untouched_holdout"] = {
            "stats": final_stats,
            "validation": validation,
            "promotion_gates": gates,
        }

        final_text_lines = [
            f"Frozen choice: {symbol} {timeframe} {chosen['strategy_key']}",
            f"Holdout start: {boundary_utc}",
            "",
            "FINAL UNTOUCHED HOLDOUT:",
            f"  trades={final_stats['closed_trades']} PF={final_stats['profit_factor']} "
            f"expR={final_stats['expectancy_r']} net={final_stats['net_pnl']} "
            f"DD_R={final_stats['max_drawdown_r']} win={final_stats['win_rate_percent']}",
            f"  promotion_passed={gates['passed']}",
            f"  failed_gates={','.join(gates['failed_gates']) if gates['failed_gates'] else 'NONE'}",
        ]
    else:
        report["frozen_choice"] = None
        final_text_lines = [
            "Frozen choice: NONE",
            "No development candidate passed the predeclared eligibility screen.",
            "Final holdout was NOT opened.",
        ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"regime_liquidity_{stamp}.json"
    text_path = out / f"regime_liquidity_{stamp}.txt"
    latest_path = out / "LATEST_REGIME_LIQUIDITY.txt"

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)

    lines = [
        "=" * 100,
        "TRADINGCORE REGIME + LIQUIDITY REVERSAL — STRICT GLOBAL HOLDOUT",
        "=" * 100,
        f"Generated UTC: {report['generated_at_utc']}",
        f"Development eligible candidates: {len(eligible)}",
        "",
        "TOP DEVELOPMENT CANDIDATES:",
    ]

    for index, item in enumerate(ranking[:12], start=1):
        s = item["stats"]
        lines.append(
            f"{index:02d}. {item['symbol']} {item['timeframe']} {item['strategy_key']} "
            f"eligible={item['development_eligible']} trades={s['closed_trades']} "
            f"PF={s['profit_factor']} expR={s['expectancy_r']} net={s['net_pnl']} "
            f"DD_R={s['max_drawdown_r']} robust={item['robustness_ratio']} "
            f"WF={item['walk_forward_passed']}"
        )

    lines.extend(["", *final_text_lines, ""])
    lines.append("RESEARCH ONLY. NO LIVE ORDER PATH. A pass still requires new forward PAPER confirmation.")

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
