#!/usr/bin/env python3
"""Exact one-month $500 scenario for the frozen BTC funding-carry candidate.

Period is supplied explicitly. Public OKX data only. The strategy is the frozen
AVG3_GT_1_5BP candidate: long BTC spot + short BTC-USDT-SWAP, equal notionals,
1x on the perp leg, entry on the next 4H bar after the signal, conservative
TradingCore fees/slippage, and actual realized funding credits after entry.

Research/PAPER only. No credentials, balances, transfers, or orders.
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

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from config.startup_safety import assert_safe_startup

OKX = "https://www.okx.com"
BAR_MS = 4 * 3_600_000
CAPITAL_USD = 500.0
LEG_NOTIONAL_USD = 250.0
LOOKBACK = 3
THRESHOLD = 0.00015
MIN_ENTRY_BASIS = -0.0025
MAX_ENTRY_BASIS = 0.02
BASIS_STOP_WIDENING = 0.025
MAX_HOLD_BARS = 180
ASSUMED_USDT_WITHDRAWAL_FEE = 1.0


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def request_json(url: str, attempts: int = 6) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "TradingCore-Monthly500/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(6.0, 0.5 * 2**attempt))
    raise RuntimeError(last)


def parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def fetch_candles(instrument: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    cursor: int | None = None
    rows: dict[int, dict[str, Any]] = {}
    for _ in range(30):
        params: dict[str, Any] = {"instId": instrument, "bar": "4H", "limit": 100}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = request_json(f"{OKX}/api/v5/market/history-candles?{urlencode(params)}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(payload)
        batch = payload.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                timestamp = int(item[0])
                oldest = timestamp if oldest is None else min(oldest, timestamp)
                confirmed = str(item[8]) == "1" if len(item) > 8 else True
                if confirmed and start_ms - 10 * 86_400_000 <= timestamp <= end_ms:
                    rows[timestamp] = {
                        "ts": timestamp,
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                    }
            except Exception:
                pass
        if oldest is None or oldest <= start_ms - 10 * 86_400_000 or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return [rows[key] for key in sorted(rows)]


def fetch_funding(start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    cursor: int | None = None
    values: dict[int, float] = {}
    for _ in range(30):
        params: dict[str, Any] = {"instId": "BTC-USDT-SWAP", "limit": 400}
        if cursor is not None:
            params["after"] = str(cursor)
        payload = request_json(f"{OKX}/api/v5/public/funding-rate-history?{urlencode(params)}")
        if str(payload.get("code")) != "0":
            raise RuntimeError(payload)
        batch = payload.get("data") or []
        if not batch:
            break
        oldest: int | None = None
        for item in batch:
            try:
                timestamp = int(item["fundingTime"])
                oldest = timestamp if oldest is None else min(oldest, timestamp)
                rate = float(item.get("realizedRate") or item.get("fundingRate"))
                if start_ms - 10 * 86_400_000 <= timestamp <= end_ms and finite(rate):
                    values[timestamp] = rate
            except Exception:
                pass
        if oldest is None or oldest <= start_ms - 10 * 86_400_000 or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.08)
    return sorted(values.items())


def align(spot: list[dict[str, Any]], perp: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spot_map = {int(row["ts"]): row for row in spot}
    perp_map = {int(row["ts"]): row for row in perp}
    return [
        {
            "ts": timestamp,
            "spot": spot_map[timestamp],
            "perp": perp_map[timestamp],
            "basis": float(perp_map[timestamp]["close"]) / float(spot_map[timestamp]["close"]) - 1.0,
        }
        for timestamp in sorted(set(spot_map) & set(perp_map))
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    safety = assert_safe_startup()
    start = parse_utc(args.start_utc)
    end = parse_utc(args.end_utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    spot = fetch_candles("BTC-USDT", start_ms, end_ms)
    perp = fetch_candles("BTC-USDT-SWAP", start_ms, end_ms)
    funding = fetch_funding(start_ms, end_ms)
    market = align(spot, perp)
    if not market:
        raise RuntimeError("No aligned OKX market data")

    config = TradingCostConfig()
    events_seen: list[tuple[int, float]] = []
    funding_index = 0
    pending_entry = False
    pending_exit = False
    position: dict[str, Any] | None = None
    signals: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []

    def close_position(timestamp: int, spot_price: float, perp_price: float, reason: str) -> None:
        nonlocal position
        if position is None:
            return
        spot_result = compute_trade_costs(
            entry_price=float(position["spot_entry"]),
            exit_price=spot_price,
            quantity=float(position["spot_quantity"]),
            side="LONG",
            config=config,
        )
        perp_result = compute_trade_costs(
            entry_price=float(position["perp_entry"]),
            exit_price=perp_price,
            quantity=float(position["perp_quantity"]),
            side="SHORT",
            config=config,
        )
        spot_net = float(spot_result["net_pnl"])
        perp_net = float(perp_result["net_pnl"])
        funding_pnl = float(position["funding_pnl"])
        total_net = spot_net + perp_net + funding_pnl
        trades.append(
            {
                "entry_utc": datetime.fromtimestamp(int(position["entry_ts"]) / 1000, tz=timezone.utc).isoformat(),
                "exit_utc": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat(),
                "reason": reason,
                "holding_bars": int(position["last_index"]) - int(position["entry_index"]),
                "spot_entry": round(float(position["spot_entry"]), 8),
                "spot_exit": round(spot_price, 8),
                "perp_entry": round(float(position["perp_entry"]), 8),
                "perp_exit": round(perp_price, 8),
                "entry_basis_pct": round(100 * float(position["entry_basis"]), 6),
                "exit_basis_pct": round(100 * (perp_price / spot_price - 1.0), 6),
                "spot_gross_pnl": spot_result["gross_pnl"],
                "perp_gross_pnl": perp_result["gross_pnl"],
                "spot_net_after_costs": spot_result["net_pnl"],
                "perp_net_after_costs": perp_result["net_pnl"],
                "funding_received": round(funding_pnl, 8),
                "trading_fees": round(float(spot_result["total_fees"]) + float(perp_result["total_fees"]), 8),
                "slippage_cost": round(float(spot_result["slippage_cost"]) + float(perp_result["slippage_cost"]), 8),
                "total_net_pnl": round(total_net, 8),
            }
        )
        position = None

    for index, row in enumerate(market):
        timestamp = int(row["ts"])
        if timestamp > end_ms:
            break
        spot_open = float(row["spot"]["open"])
        perp_open = float(row["perp"]["open"])

        if pending_exit and position is not None:
            close_position(timestamp, spot_open, perp_open, str(position.get("exit_reason") or "EXIT"))
            pending_exit = False

        if pending_entry and position is None and timestamp >= start_ms:
            position = {
                "entry_ts": timestamp,
                "entry_index": index,
                "last_index": index,
                "spot_entry": spot_open,
                "perp_entry": perp_open,
                "spot_quantity": LEG_NOTIONAL_USD / spot_open,
                "perp_quantity": LEG_NOTIONAL_USD / perp_open,
                "entry_basis": perp_open / spot_open - 1.0,
                "funding_pnl": 0.0,
            }
            pending_entry = False

        bar_close = timestamp + BAR_MS - 1
        while funding_index < len(funding) and funding[funding_index][0] <= bar_close:
            event_timestamp, rate = funding[funding_index]
            events_seen.append((event_timestamp, rate))
            if position is not None and event_timestamp > int(position["entry_ts"]):
                position["funding_pnl"] = float(position["funding_pnl"]) + LEG_NOTIONAL_USD * float(rate)
            funding_index += 1

        recent = [rate for _, rate in events_seen[-LOOKBACK:]]
        rolling = sum(recent) / LOOKBACK if len(recent) == LOOKBACK else None
        basis = float(row["perp"]["close"]) / float(row["spot"]["close"]) - 1.0

        if position is not None:
            position["last_index"] = index
            holding = index - int(position["entry_index"])
            if holding >= MAX_HOLD_BARS:
                position["exit_reason"] = "MAX_HOLD"
                pending_exit = True
            elif basis - float(position["entry_basis"]) >= BASIS_STOP_WIDENING:
                position["exit_reason"] = "BASIS_STOP"
                pending_exit = True
            elif finite(rolling) and float(rolling) <= 0:
                position["exit_reason"] = "FUNDING_DECAY"
                pending_exit = True

        if position is None and not pending_entry and not pending_exit and start_ms <= bar_close <= end_ms:
            if finite(rolling) and float(rolling) >= THRESHOLD and MIN_ENTRY_BASIS <= basis <= MAX_ENTRY_BASIS and index + 1 < len(market):
                signals.append(
                    {
                        "signal_utc": datetime.fromtimestamp(bar_close / 1000, tz=timezone.utc).isoformat(),
                        "avg_last_3_funding": rolling,
                        "avg_last_3_funding_bps": round(float(rolling) * 10_000, 6),
                        "basis_pct": round(100 * basis, 6),
                    }
                )
                pending_entry = True

    if position is not None:
        eligible = [row for row in market if int(row["ts"]) <= end_ms]
        last = eligible[-1]
        close_position(
            end_ms,
            float(last["spot"]["close"]),
            float(last["perp"]["close"]),
            "PERIOD_END_MARK_TO_MARKET",
        )

    strategy_net = round(sum(float(trade["total_net_pnl"]) for trade in trades), 8)
    ending_before_transfer = round(CAPITAL_USD + strategy_net, 8)
    ending_after_assumed_withdrawal = round(max(0.0, ending_before_transfer - ASSUMED_USDT_WITHDRAWAL_FEE), 8)

    report = {
        "schema": "TRADINGCORE_MONTHLY_500_FUNDING_SCENARIO_V1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_start_utc": start.isoformat(),
        "period_end_utc": end.isoformat(),
        "exchange": "OKX public historical data",
        "strategy": "BTC delta-neutral funding carry AVG3_GT_1_5BP",
        "initial_capital_usd": CAPITAL_USD,
        "spot_leg_usd": LEG_NOTIONAL_USD,
        "perp_short_notional_usd": LEG_NOTIONAL_USD,
        "leverage_on_perp_leg": 1.0,
        "signal_count": len(signals),
        "closed_or_marked_trades": len(trades),
        "signals": signals,
        "trades": trades,
        "strategy_net_pnl_after_trading_costs_usd": strategy_net,
        "ending_balance_before_external_transfer_fees_usd": ending_before_transfer,
        "external_fee_assumption": {
            "crypto_deposit_fee_usd": 0.0,
            "usdt_withdrawal_network_fee_usd": ASSUMED_USDT_WITHDRAWAL_FEE,
            "bank_or_card_conversion_fee_included": False,
        },
        "ending_cash_after_assumed_withdrawal_usd": ending_after_assumed_withdrawal,
        "net_cash_profit_after_assumed_withdrawal_usd": round(ending_after_assumed_withdrawal - CAPITAL_USD, 8),
        "cost_model": config.snapshot(),
        "safety": safety,
        "private_api_used": False,
        "real_orders_enabled": False,
        "live_permission": False,
        "note": "Historical what-if, not a guarantee. Bank/card conversion fees vary and are not included.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
