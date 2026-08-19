#!/usr/bin/env python3
"""TradingCore Cost-Aware Hourly ML Lab V1.

A fixed XGBoost regression model predicts the six-hour BTCUSDT move using only
lagged hourly features. Signals are converted to trades only when the predicted
move clears a frozen 35-bp execution hurdle. Evaluation is rolling walk-forward,
with purged train/test boundaries, next-bar-open entries, conservative costs,
adverse stop-first intrabar handling, and independent Binance/OKX runs.

Research/PAPER only. No private API, balances, credentials, or order path.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from config.startup_safety import assert_safe_startup

SCHEMA = "TRADINGCORE_COST_AWARE_HOURLY_ML_V1"
BINANCE = "https://data-api.binance.vision"
OKX = "https://www.okx.com"
SYMBOL = "BTCUSDT"
YEARS = 4
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
HORIZON_BARS = 6
TRAIN_DAYS = 730
TEST_DAYS = 90
PURGE_BARS = HORIZON_BARS + 2
PREDICTION_HURDLE = 0.0035
CAPITAL_USD = 1000.0
RISK_USD = 1.0
FEATURES = (
    "ret_1", "ret_3", "ret_6", "ret_12", "ret_24", "ret_72", "ret_168",
    "ema12_gap", "ema48_gap", "ema168_gap",
    "vol_24", "vol_72", "vol_168", "atr14_pct", "rsi14",
    "volume_z24", "volume_z168", "range_pct", "close_location",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
)


def finite(value: Any) -> bool:
    return isinstance(value, (int, float, np.floating)) and math.isfinite(float(value))


def req(url: str, attempts: int = 6) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "TradingCore-CostAwareML/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError(last)


def bounds() -> tuple[int, int]:
    now = int(time.time() * 1000)
    end = (now // HOUR_MS) * HOUR_MS - 1
    return end - int(YEARS * 365.25 * DAY_MS), end


def fetch_binance() -> list[dict[str, float | int]]:
    start, end = bounds()
    cursor = start
    rows: dict[int, dict[str, float | int]] = {}
    while cursor <= end:
        query = urlencode({"symbol": SYMBOL, "interval": "1h", "startTime": cursor, "endTime": end, "limit": 1000})
        data = req(f"{BINANCE}/api/v3/klines?{query}")
        if not isinstance(data, list) or not data:
            break
        last_open: int | None = None
        for item in data:
            try:
                timestamp = int(item[0])
                close_timestamp = int(item[6])
                last_open = timestamp
                if start <= timestamp and close_timestamp <= end:
                    rows[timestamp] = {
                        "ts": timestamp,
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                    }
            except Exception:
                pass
        if last_open is None or len(data) < 1000:
            break
        nxt = last_open + HOUR_MS
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.015)
    return [rows[key] for key in sorted(rows)]


def fetch_okx() -> list[dict[str, float | int]]:
    start, end = bounds()
    cursor: int | None = None
    rows: dict[int, dict[str, float | int]] = {}
    for _ in range(700):
        params: dict[str, Any] = {"instId": "BTC-USDT", "bar": "1H", "limit": 100}
        if cursor is not None:
            params["after"] = str(cursor)
        data = req(f"{OKX}/api/v5/market/history-candles?{urlencode(params)}")
        if not isinstance(data, dict) or str(data.get("code")) != "0":
            raise RuntimeError(data)
        batch = data.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                timestamp = int(item[0])
                oldest = timestamp if oldest is None else min(oldest, timestamp)
                confirmed = str(item[8]) == "1" if len(item) > 8 else True
                if confirmed and start <= timestamp <= end:
                    rows[timestamp] = {
                        "ts": timestamp,
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                    }
            except Exception:
                pass
        if oldest is None or oldest <= start or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return [rows[key] for key in sorted(rows)]


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    ratio = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + ratio)


def make_frame(rows: list[dict[str, float | int]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    close = frame["close"].astype(float)
    returns = close.pct_change()
    for lag in (1, 3, 6, 12, 24, 72, 168):
        frame[f"ret_{lag}"] = close.pct_change(lag)
    for span in (12, 48, 168):
        ema = close.ewm(span=span, adjust=False).mean()
        frame[f"ema{span}_gap"] = close / ema - 1
    for window in (24, 72, 168):
        frame[f"vol_{window}"] = returns.rolling(window).std() * math.sqrt(window)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    frame["atr14_pct"] = frame["atr14"] / close
    frame["rsi14"] = rsi(close)
    for window in (24, 168):
        mean = frame["volume"].rolling(window).mean()
        std = frame["volume"].rolling(window).std()
        frame[f"volume_z{window}"] = (frame["volume"] - mean) / std.replace(0, np.nan)
    frame["range_pct"] = (frame["high"] - frame["low"]) / close
    width = (frame["high"] - frame["low"]).replace(0, np.nan)
    frame["close_location"] = (frame["close"] - frame["low"]) / width
    dt = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    hour = dt.dt.hour.astype(float)
    day = dt.dt.dayofweek.astype(float)
    frame["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    frame["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    frame["dow_sin"] = np.sin(2 * np.pi * day / 7)
    frame["dow_cos"] = np.cos(2 * np.pi * day / 7)
    frame["entry_price"] = frame["open"].shift(-1)
    frame["future_exit"] = frame["close"].shift(-HORIZON_BARS)
    frame["target_return"] = frame["future_exit"] / frame["entry_price"] - 1
    frame["row_index"] = np.arange(len(frame))
    return frame.replace([np.inf, -np.inf], np.nan)


def trade_stats(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    wins = [value for value in rs if value > 0]
    losses = [value for value in rs if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = peak = max_dd = 0.0
    for value in rs:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(100 * len(wins) / n, 2) if n else None,
        "net_r": round(sum(rs), 4) if n else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else (99.0 if wins else None),
        "expectancy_r": round(sum(rs) / n, 4) if n else None,
        "max_drawdown_r": round(max_dd, 4) if n else None,
    }


def simulate_predictions(raw: pd.DataFrame, test: pd.DataFrame, predictions: np.ndarray) -> dict[str, Any]:
    config = TradingCostConfig()
    rs: list[float] = []
    trades: list[dict[str, Any]] = []
    last_exit_index = -1
    candidates = test.copy()
    candidates["prediction"] = predictions
    for row in candidates.itertuples(index=False):
        decision_index = int(row.row_index)
        if decision_index <= last_exit_index or float(row.prediction) <= PREDICTION_HURDLE:
            continue
        entry_index = decision_index + 1
        final_index = decision_index + HORIZON_BARS
        if final_index >= len(raw):
            continue
        entry = float(raw.iloc[entry_index]["open"])
        atr_value = float(raw.iloc[decision_index]["atr14"])
        if not finite(atr_value) or atr_value <= 0:
            continue
        stop = entry - 1.5 * atr_value
        unit_risk = entry - stop
        if stop <= 0 or unit_risk <= 0:
            continue
        quantity = min(RISK_USD / unit_risk, CAPITAL_USD / entry)
        risk = quantity * unit_risk
        if quantity <= 0 or risk <= 0:
            continue
        exit_price: float | None = None
        exit_reason = "TIME"
        exit_index = final_index
        for bar_index in range(entry_index, final_index + 1):
            bar = raw.iloc[bar_index]
            if float(bar["open"]) <= stop:
                exit_price = float(bar["open"])
                exit_reason = "GAP_STOP"
                exit_index = bar_index
                break
            if float(bar["low"]) <= stop:
                exit_price = stop
                exit_reason = "STOP"
                exit_index = bar_index
                break
        if exit_price is None:
            exit_price = float(raw.iloc[final_index]["close"])
        costs = compute_trade_costs(
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
            side="LONG",
            config=config,
        )
        r_value = float(costs["net_pnl"]) / risk
        rs.append(r_value)
        trades.append(
            {
                "decision_utc": datetime.fromtimestamp(int(row.ts) / 1000, tz=timezone.utc).isoformat(),
                "prediction_pct": round(100 * float(row.prediction), 4),
                "r": round(r_value, 5),
                "reason": exit_reason,
            }
        )
        last_exit_index = exit_index
    return {"stats": trade_stats(rs), "trades": trades}


def model() -> XGBRegressor:
    return XGBRegressor(
        n_estimators=350,
        max_depth=3,
        learning_rate=0.03,
        min_child_weight=20,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=10.0,
        objective="reg:squarederror",
        tree_method="hist",
        n_jobs=2,
        random_state=137,
    )


def run_walk_forward(rows: list[dict[str, float | int]]) -> dict[str, Any]:
    raw = make_frame(rows)
    clean = raw.dropna(subset=[*FEATURES, "target_return", "entry_price", "atr14"]).copy()
    if len(clean) < TRAIN_DAYS * 24 + TEST_DAYS * 24:
        return {"error": "INSUFFICIENT_CLEAN_DATA", "rows": len(clean), "passed": False}
    first_ts = int(clean["ts"].min())
    last_ts = int(clean["ts"].max())
    test_start = first_ts + TRAIN_DAYS * DAY_MS
    folds: list[dict[str, Any]] = []
    all_rs: list[float] = []
    all_trades: list[dict[str, Any]] = []
    importances: list[np.ndarray] = []

    while test_start + TEST_DAYS * DAY_MS <= last_ts:
        train_start = test_start - TRAIN_DAYS * DAY_MS
        train_end = test_start - PURGE_BARS * HOUR_MS
        test_end = test_start + TEST_DAYS * DAY_MS
        train = clean[(clean["ts"] >= train_start) & (clean["ts"] < train_end)]
        test = clean[(clean["ts"] >= test_start) & (clean["ts"] < test_end)]
        if len(train) < 10_000 or len(test) < 1_000:
            test_start = test_end
            continue
        estimator = model()
        estimator.fit(train[list(FEATURES)], np.clip(train["target_return"].to_numpy(), -0.15, 0.15))
        predictions = estimator.predict(test[list(FEATURES)])
        simulated = simulate_predictions(raw, test, predictions)
        fold_stats = simulated["stats"]
        folds.append(
            {
                "test_start_utc": datetime.fromtimestamp(test_start / 1000, tz=timezone.utc).isoformat(),
                "test_end_utc": datetime.fromtimestamp(test_end / 1000, tz=timezone.utc).isoformat(),
                "train_rows": len(train),
                "test_rows": len(test),
                "stats": fold_stats,
            }
        )
        all_rs.extend(float(item["r"]) for item in simulated["trades"])
        all_trades.extend(simulated["trades"])
        importances.append(np.asarray(estimator.feature_importances_, dtype=float))
        test_start = test_end

    full = trade_stats(all_rs)
    positive_folds = sum(
        1 for fold in folds
        if finite(fold["stats"].get("net_r")) and float(fold["stats"]["net_r"]) > 0
    )
    positive_fold_ratio = positive_folds / len(folds) if folds else 0.0
    recent_three_positive = bool(
        len(folds) >= 3
        and sum(float(fold["stats"].get("net_r") or 0.0) for fold in folds[-3:]) > 0
    )
    checks = {
        "trades": int(full.get("closed_trades") or 0) >= 30,
        "profit_factor": finite(full.get("profit_factor")) and float(full["profit_factor"]) >= 1.20,
        "expectancy": finite(full.get("expectancy_r")) and float(full["expectancy_r"]) >= 0.05,
        "drawdown": finite(full.get("max_drawdown_r")) and float(full["max_drawdown_r"]) <= 10.0,
        "positive_fold_ratio": positive_fold_ratio >= 0.60,
        "recent_three_positive": recent_three_positive,
    }
    mean_importance = np.mean(np.vstack(importances), axis=0) if importances else np.zeros(len(FEATURES))
    importance_rows = sorted(
        ({"feature": feature, "importance": round(float(value), 6)} for feature, value in zip(FEATURES, mean_importance)),
        key=lambda item: item["importance"],
        reverse=True,
    )
    return {
        "full": full,
        "fold_count": len(folds),
        "positive_folds": positive_folds,
        "positive_fold_ratio": round(positive_fold_ratio, 4),
        "recent_three_positive": recent_three_positive,
        "checks": checks,
        "passed": all(checks.values()),
        "folds": folds,
        "top_features": importance_rows[:10],
        "recent_trades": all_trades[-20:],
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="cost_aware_ml_runtime")
    args = parser.parse_args()
    safety = assert_safe_startup()
    venue_results: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for venue, loader in (("BINANCE", fetch_binance), ("OKX", fetch_okx)):
        try:
            rows = loader()
            print("COST_AWARE_DATA", venue, "bars=", len(rows), flush=True)
            venue_results[venue] = run_walk_forward(rows)
        except Exception as exc:
            failures[venue] = f"{type(exc).__name__}: {exc}"
            venue_results[venue] = {"error": failures[venue], "passed": False}
    passed_both = all(bool(venue_results.get(venue, {}).get("passed")) for venue in ("BINANCE", "OKX"))
    state = "COST_AWARE_ML_CANDIDATE_FOUND_FORWARD_REQUIRED" if passed_both else "NO_COST_AWARE_ML_CANDIDATE"
    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "symbol": SYMBOL,
        "history_years": YEARS,
        "horizon_bars": HORIZON_BARS,
        "prediction_hurdle": PREDICTION_HURDLE,
        "train_days": TRAIN_DAYS,
        "test_days": TEST_DAYS,
        "features": list(FEATURES),
        "model": "Fixed XGBoost regression; no hyperparameter search",
        "execution_model": "next-bar-open; 1.5 ATR stop; six-hour time exit; conservative TradingCore costs; non-overlapping long-only trades",
        "venues": venue_results,
        "passed_both_venues": passed_both,
        "data_failures": failures,
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "live_permission": False,
        "note": "Walk-forward evidence only. A pass still requires a newly frozen forward execution test before micro-live review.",
    }
    output = Path(args.output_dir)
    atomic_json(output / "COST_AWARE_HOURLY_ML_RESULT.json", report)
    print("COST_AWARE_ML", state, "both=", passed_both, "failures=", failures, "LIVE=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
