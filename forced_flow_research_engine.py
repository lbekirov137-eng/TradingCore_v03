#!/usr/bin/env python3
"""Preregistered forced-flow research engine.

READS Collector B evidence and PUBLIC Bybit 1m klines only.
NO exchange credentials, NO order client, NO LIVE path, NO parameter search.
The complete hypothesis/geometry is frozen in forced_flow_protocol.py.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_supervisor.gates import promotion_gates
from api.strategy_supervisor.stats import ClosedTrade, build_stats
from api.strategy_supervisor.validation import validate_candidate
from config.startup_safety import assert_safe_startup

import forced_flow_protocol as protocol


SCHEMA = "TRADINGCORE_FORCED_FLOW_RESEARCH_V1"
BYBIT_REST = "https://api.bybit.com"
MINUTE_MS = 60_000


@dataclass(frozen=True)
class LiqEvent:
    event_ts_ms: int
    symbol: str
    side: str
    notional_usdt: float
    event_key: str


@dataclass(frozen=True)
class Cluster:
    symbol: str
    side: str
    start_ms: int
    end_ms: int
    event_count: int
    aggregate_notional_usdt: float
    keys: tuple[str, ...]


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def read_events(data_dir: Path) -> list[LiqEvent]:
    root = data_dir / "normalized" / "bybit"
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    events: list[LiqEvent] = []
    seen: set[str] = set()

    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("cohort") != protocol.COHORT:
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if symbol not in protocol.SYMBOLS:
                    continue
                side = str(row.get("liquidated_position_side") or "").upper()
                if side not in ("LONG", "SHORT"):
                    continue
                event_ts = row.get("event_ts_ms")
                if not isinstance(event_ts, int) or event_ts <= 0:
                    continue
                size = _finite_positive(row.get("size_raw"))
                price = _finite_positive(row.get("bankruptcy_price_raw"))
                key = str(row.get("event_key") or "")
                if size is None or price is None or not key or key in seen:
                    continue
                seen.add(key)
                events.append(
                    LiqEvent(
                        event_ts_ms=event_ts,
                        symbol=symbol,
                        side=side,
                        notional_usdt=size * price,
                        event_key=key,
                    )
                )

    return sorted(events, key=lambda e: (e.event_ts_ms, e.symbol, e.side, e.event_key))


def build_clusters(events: list[LiqEvent]) -> list[Cluster]:
    window_ms = protocol.CLUSTER_WINDOW_SECONDS * 1000
    groups: dict[tuple[str, str], list[LiqEvent]] = {}
    for event in events:
        groups.setdefault((event.symbol, event.side), []).append(event)

    clusters: list[Cluster] = []
    for (symbol, side), rows in groups.items():
        rows.sort(key=lambda e: e.event_ts_ms)
        current: list[LiqEvent] = []
        for event in rows:
            if not current:
                current = [event]
                continue
            # Fixed-chain cluster: every subsequent event must arrive within 60s
            # of the immediately previous same-symbol/same-side event.
            if event.event_ts_ms - current[-1].event_ts_ms <= window_ms:
                current.append(event)
            else:
                clusters.append(_cluster(symbol, side, current))
                current = [event]
        if current:
            clusters.append(_cluster(symbol, side, current))

    return sorted(clusters, key=lambda c: (c.end_ms, c.symbol, c.side))


def _cluster(symbol: str, side: str, rows: list[LiqEvent]) -> Cluster:
    return Cluster(
        symbol=symbol,
        side=side,
        start_ms=rows[0].event_ts_ms,
        end_ms=rows[-1].event_ts_ms,
        event_count=len(rows),
        aggregate_notional_usdt=sum(row.notional_usdt for row in rows),
        keys=tuple(row.event_key for row in rows),
    )


def _http_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{BYBIT_REST}{path}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "TradingCore-ForcedFlow-Research/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit public market request failed: {payload}")
    return payload


def fetch_1m_klines(symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
    """Fetch public historical linear klines, paginating backward."""
    cursor_end = end_ms
    rows_by_time: dict[int, Candle] = {}
    for _ in range(500):
        payload = _http_json(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": protocol.PRICE_INTERVAL,
                "start": max(0, start_ms),
                "end": cursor_end,
                "limit": 1000,
            },
        )
        result = payload.get("result") or {}
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        oldest = None
        for raw in rows:
            if not isinstance(raw, list) or len(raw) < 5:
                continue
            try:
                candle = Candle(
                    start_ms=int(raw[0]),
                    open=float(raw[1]),
                    high=float(raw[2]),
                    low=float(raw[3]),
                    close=float(raw[4]),
                )
            except (TypeError, ValueError):
                continue
            if candle.start_ms < start_ms or candle.start_ms > end_ms:
                continue
            rows_by_time[candle.start_ms] = candle
            oldest = candle.start_ms if oldest is None else min(oldest, candle.start_ms)
        if oldest is None or oldest <= start_ms:
            break
        cursor_end = oldest - 1
        time.sleep(0.06)

    return sorted(rows_by_time.values(), key=lambda c: c.start_ms)


def atr14(prior: list[Candle]) -> float | None:
    if len(prior) < protocol.ATR_PERIOD + 1:
        return None
    rows = prior[-(protocol.ATR_PERIOD + 1):]
    trs: list[float] = []
    for index in range(1, len(rows)):
        current = rows[index]
        previous = rows[index - 1]
        tr = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if len(trs) == protocol.ATR_PERIOD else None


def ceil_minute(ms: int) -> int:
    return ((ms + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS


def simulate_threshold(
    clusters: list[Cluster],
    prices: dict[str, list[Candle]],
    threshold: float,
) -> tuple[list[ClosedTrade], list[dict[str, Any]]]:
    qualifying = [
        c for c in clusters
        if c.side == protocol.LIQUIDATED_SIDE
        and c.aggregate_notional_usdt >= threshold
    ]
    qualifying.sort(key=lambda c: c.end_ms)

    indexes = {
        symbol: {candle.start_ms: index for index, candle in enumerate(rows)}
        for symbol, rows in prices.items()
    }
    busy_until: dict[str, int] = {symbol: 0 for symbol in protocol.SYMBOLS}
    trades: list[ClosedTrade] = []
    details: list[dict[str, Any]] = []
    costs = TradingCostConfig()

    for cluster in qualifying:
        if cluster.end_ms < busy_until.get(cluster.symbol, 0):
            continue
        rows = prices.get(cluster.symbol) or []
        by_time = indexes.get(cluster.symbol) or {}
        stabilisation_start = ceil_minute(cluster.end_ms)
        stabilisation_index = by_time.get(stabilisation_start)
        if stabilisation_index is None or stabilisation_index < protocol.ATR_PERIOD + 1:
            continue
        entry_index = stabilisation_index + protocol.STABILISATION_CANDLES
        if entry_index >= len(rows):
            continue
        stabilisation = rows[stabilisation_index]
        if not stabilisation.close > stabilisation.open:
            continue

        atr_value = atr14(rows[:stabilisation_index])
        if atr_value is None or atr_value <= 0:
            continue

        entry_candle = rows[entry_index]
        entry = float(entry_candle.open)
        stop = entry - protocol.ATR_STOP_MULTIPLE * atr_value
        if stop <= 0 or stop >= entry:
            continue
        risk_per_unit = entry - stop
        quantity = protocol.RISK_AMOUNT_USD / risk_per_unit
        notional = quantity * entry
        if notional > protocol.REFERENCE_CAPITAL_USD * protocol.MAX_LEVERAGE + 1e-9:
            continue
        target = entry + protocol.RISK_REWARD * risk_per_unit

        exit_price = None
        exit_reason = None
        exit_index = None
        end_index = min(len(rows) - 1, entry_index + protocol.MAX_HOLD_MINUTES - 1)
        for index in range(entry_index, end_index + 1):
            candle = rows[index]
            # Conservative same-candle ordering: stop first.
            if candle.low <= stop:
                exit_price = stop
                exit_reason = "STOP_LOSS"
                exit_index = index
                break
            if candle.high >= target:
                exit_price = target
                exit_reason = "TAKE_PROFIT"
                exit_index = index
                break
        if exit_price is None:
            exit_index = end_index
            exit_price = rows[exit_index].close
            exit_reason = "TIME_STOP"

        result = compute_trade_costs(
            entry_price=entry,
            exit_price=float(exit_price),
            quantity=quantity,
            side="LONG",
            config=costs,
        )
        net_pnl = float(result["net_pnl"])
        r_multiple = net_pnl / protocol.RISK_AMOUNT_USD
        closed_ms = rows[exit_index].start_ms + MINUTE_MS
        trade = ClosedTrade(
            strategy_id=protocol.STRATEGY_ID,
            closed_at_utc=utc(closed_ms),
            regime=cluster.symbol,
            net_pnl=net_pnl,
            r_multiple=r_multiple,
        )
        trades.append(trade)
        busy_until[cluster.symbol] = closed_ms
        details.append(
            {
                "symbol": cluster.symbol,
                "cluster_start_utc": utc(cluster.start_ms),
                "cluster_end_utc": utc(cluster.end_ms),
                "cluster_events": cluster.event_count,
                "cluster_notional_usdt": round(cluster.aggregate_notional_usdt, 2),
                "threshold_usdt": threshold,
                "stabilisation_start_utc": utc(stabilisation_start),
                "entry": entry,
                "stop": stop,
                "target": target,
                "quantity": quantity,
                "position_notional": notional,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "closed_at_utc": utc(closed_ms),
                "net_pnl": net_pnl,
                "r_multiple": r_multiple,
                "real_order_sent": False,
            }
        )

    return trades, details


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="C:/TradingCore_Collector_B/data")
    parser.add_argument("--output", default="forced_flow_research_results")
    args = parser.parse_args()

    safety = assert_safe_startup()
    data_dir = Path(args.data_dir)
    out = Path(args.output)
    if not out.is_absolute():
        out = Path.cwd() / out
    out.mkdir(parents=True, exist_ok=True)

    events = read_events(data_dir)
    clusters = build_clusters(events)
    long_clusters = [c for c in clusters if c.side == protocol.LIQUIDATED_SIDE]
    primary_clusters = [c for c in long_clusters if c.aggregate_notional_usdt >= protocol.PRIMARY_THRESHOLD_USDT]

    span_hours = 0.0
    if len(events) >= 2:
        span_hours = (events[-1].event_ts_ms - events[0].event_ts_ms) / 3_600_000.0

    readiness_missing: list[str] = []
    if len(events) < protocol.MIN_VALID_EVENTS_FOR_RESEARCH:
        readiness_missing.append(f"events={len(events)}<{protocol.MIN_VALID_EVENTS_FOR_RESEARCH}")
    if span_hours < protocol.MIN_OBSERVATION_SPAN_HOURS:
        readiness_missing.append(f"span_hours={span_hours:.2f}<{protocol.MIN_OBSERVATION_SPAN_HOURS}")
    if len(primary_clusters) < protocol.MIN_PRIMARY_CLUSTERS_FOR_RESEARCH:
        readiness_missing.append(
            f"primary_long_clusters={len(primary_clusters)}<{protocol.MIN_PRIMARY_CLUSTERS_FOR_RESEARCH}"
        )

    base_report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "RESEARCH_ONLY",
        "protocol": {**protocol.protocol_dict(), "fingerprint": protocol.PROTOCOL_FINGERPRINT},
        "safety": safety,
        "real_orders_enabled": False,
        "real_order_sent": False,
        "private_api_used": False,
        "collector_a_modified": False,
        "event_count": len(events),
        "observation_span_hours": round(span_hours, 4),
        "all_clusters": len(clusters),
        "long_clusters": len(long_clusters),
        "primary_long_clusters": len(primary_clusters),
        "readiness_missing": readiness_missing,
    }

    if readiness_missing:
        base_report["state"] = "WAITING_FOR_PREREGISTERED_SAMPLE"
        _write(out, base_report)
        return 0

    # Price data is public and fetched only after the preregistered sample gate.
    prices: dict[str, list[Candle]] = {}
    for symbol in protocol.SYMBOLS:
        relevant = [c for c in primary_clusters if c.symbol == symbol]
        if not relevant:
            prices[symbol] = []
            continue
        start_ms = min(c.end_ms for c in relevant) - 2 * 60 * MINUTE_MS
        end_ms = max(c.end_ms for c in relevant) + 2 * 60 * MINUTE_MS
        # Do not include a potentially still-open current candle.
        end_ms = min(end_ms, int(time.time() * 1000) - MINUTE_MS)
        prices[symbol] = fetch_1m_klines(symbol, start_ms, end_ms)

    threshold_results: dict[str, Any] = {}
    primary_trades: list[ClosedTrade] = []
    primary_details: list[dict[str, Any]] = []

    for threshold in protocol.THRESHOLD_COHORTS_USDT:
        trades, details = simulate_threshold(clusters, prices, threshold)
        stats = build_stats(trades)
        threshold_results[str(int(threshold))] = {
            "threshold_usdt": threshold,
            "trades": len(trades),
            "stats_full_sample_descriptive": stats,
        }
        if threshold == protocol.PRIMARY_THRESHOLD_USDT:
            primary_trades = trades
            primary_details = details

    if len(primary_trades) < 2:
        base_report.update(
            state="INSUFFICIENT_TRADE_GEOMETRY",
            threshold_results=threshold_results,
            primary_trade_count=len(primary_trades),
        )
        _write(out, base_report)
        return 0

    sample_id = (
        f"{protocol.PROTOCOL_VERSION}:{protocol.PROTOCOL_FINGERPRINT[:16]}:"
        f"{events[0].event_ts_ms}:{events[-1].event_ts_ms}"
    )
    validation = validate_candidate(
        protocol.STRATEGY_ID,
        primary_trades,
        sample_id=sample_id,
        holdout_fraction=protocol.HOLDOUT_FRACTION,
        window_count=4,
        safety_violations=(),
    )

    holdout_start = validation.get("holdout_start_utc")
    cross_threshold_profitable = 0
    cross_threshold_oos: dict[str, Any] = {}
    for threshold in protocol.THRESHOLD_COHORTS_USDT:
        trades, _ = simulate_threshold(clusters, prices, threshold)
        oos = [
            trade for trade in trades
            if isinstance(trade.closed_at_utc, str)
            and isinstance(holdout_start, str)
            and trade.closed_at_utc >= holdout_start
        ]
        stats = build_stats(oos)
        profitable = bool(
            stats.get("net_pnl") is not None and stats["net_pnl"] > 0
            and stats.get("expectancy_r") is not None and stats["expectancy_r"] > 0
        )
        if profitable:
            cross_threshold_profitable += 1
        cross_threshold_oos[str(int(threshold))] = {
            "trades": len(oos),
            "stats": stats,
            "profitable": profitable,
        }

    cross_ratio = cross_threshold_profitable / len(protocol.THRESHOLD_COHORTS_USDT)
    wf_ratio = validation.get("robustness_ratio")
    effective_ratio = (
        min(float(wf_ratio), cross_ratio)
        if isinstance(wf_ratio, (int, float))
        else cross_ratio
    )
    validation["robustness_ratio"] = round(effective_ratio, 4)
    validation["cross_threshold_robustness_ratio"] = round(cross_ratio, 4)
    validation["cross_threshold_profitable_cohorts"] = cross_threshold_profitable
    validation["cross_threshold_required_profitable_cohorts"] = protocol.MIN_PROFITABLE_THRESHOLD_COHORTS

    gates = promotion_gates(validation)
    if cross_threshold_profitable < protocol.MIN_PROFITABLE_THRESHOLD_COHORTS:
        gates["passed"] = False
        if "cross_threshold_robustness" not in gates["failed_gates"]:
            gates["failed_gates"].append("cross_threshold_robustness")
        gates["checks"].append(
            {
                "gate": "cross_threshold_robustness",
                "passed": False,
                "detail": (
                    f"{cross_threshold_profitable}/{len(protocol.THRESHOLD_COHORTS_USDT)} "
                    f"profitable OOS threshold cohorts; required "
                    f"{protocol.MIN_PROFITABLE_THRESHOLD_COHORTS}"
                ),
            }
        )
    else:
        gates["checks"].append(
            {
                "gate": "cross_threshold_robustness",
                "passed": True,
                "detail": f"{cross_threshold_profitable}/{len(protocol.THRESHOLD_COHORTS_USDT)} profitable OOS cohorts",
            }
        )

    state = "HISTORICAL_PROMOTION_PASS" if gates["passed"] else "HISTORICAL_REJECT_OR_MORE_DATA"
    report = {
        **base_report,
        "state": state,
        "sample_id": sample_id,
        "price_source": "BYBIT_PUBLIC_LINEAR_1M_NO_API_KEY",
        "primary_trade_count": len(primary_trades),
        "threshold_results": threshold_results,
        "cross_threshold_oos": cross_threshold_oos,
        "validation": validation,
        "promotion_gates": gates,
        "primary_trade_details": primary_details,
        "next_gate": (
            "FORWARD_PAPER_CONFIRMATION"
            if gates["passed"]
            else "NEW_INDEPENDENT_SAMPLE_REQUIRED_OR_PROTOCOL_V2"
        ),
        "note": "Historical PASS is not LIVE permission. Forward PAPER is mandatory.",
    }
    _write(out, report)
    return 0


def _write(out: Path, report: dict[str, Any]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"forced_flow_research_{stamp}.json"
    latest_json = out / "LATEST_FORCED_FLOW_RESEARCH.json"
    latest_txt = out / "LATEST_FORCED_FLOW_RESEARCH.txt"
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    json_path.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")

    gates = report.get("promotion_gates") or {}
    validation = report.get("validation") or {}
    lines = [
        "=" * 92,
        "TRADINGCORE FORCED-FLOW PREREGISTERED RESEARCH",
        "=" * 92,
        f"Generated UTC: {report.get('generated_at_utc')}",
        f"State: {report.get('state')}",
        f"Protocol: {protocol.PROTOCOL_VERSION} fingerprint={protocol.PROTOCOL_FINGERPRINT}",
        f"Events: {report.get('event_count')} span_hours={report.get('observation_span_hours')}",
        f"Primary long clusters: {report.get('primary_long_clusters')}",
        f"Primary trades: {report.get('primary_trade_count')}",
        f"OOS trades: {validation.get('oos_trades')} PF={validation.get('oos_profit_factor')} expR={validation.get('oos_expectancy_r')} net={validation.get('oos_net_pnl')} DD_R={validation.get('oos_max_drawdown_r')}",
        f"Promotion passed: {gates.get('passed')}",
        f"Failed gates: {','.join(gates.get('failed_gates') or []) if gates else 'N/A'}",
        f"Readiness missing: {','.join(report.get('readiness_missing') or []) or 'NONE'}",
        "Collector A: UNCHANGED | PRIVATE API: NO | ORDERS: DISABLED | LIVE: DISABLED",
    ]
    latest_txt.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print("JSON:", json_path, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
