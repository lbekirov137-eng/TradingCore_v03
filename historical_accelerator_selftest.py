#!/usr/bin/env python3
"""Fail-closed self-test for Historical Accelerator V1."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from config.startup_safety import assert_safe_startup
import historical_accelerator_protocol as protocol
import historical_accelerator as engine


def main() -> int:
    safety = assert_safe_startup()
    problems: list[str] = []

    if safety.get("live_trading") is not False:
        problems.append("startup safety is not LIVE=false")
    if protocol.MAX_LEVERAGE != 1.0:
        problems.append("protocol leverage is not 1x")
    if protocol.RISK_AMOUNT_USD != 1.0 or protocol.REFERENCE_CAPITAL_USD != 1000.0:
        problems.append("risk contract changed")
    if len(protocol.FAMILIES) != 3:
        problems.append("expected exactly three preregistered families")
    if protocol.ACCELERATOR_MIN_OOS_TRADES < 50:
        problems.append("accelerator OOS gate weakened")
    if protocol.ACCELERATOR_MIN_OOS_PROFIT_FACTOR < 1.25:
        problems.append("accelerator PF gate weakened")
    if protocol.MIN_PROFITABLE_SYMBOL_RATIO < 0.60:
        problems.append("cross-symbol gate weakened")

    source = inspect.getsource(engine).lower()
    forbidden = (
        "place_order", "create_order", "submit_order", "private_api_key",
        "api_secret", "wallet_balance", "position_mode", "set_leverage",
    )
    for token in forbidden:
        if token in source:
            problems.append(f"forbidden execution/private token present: {token}")

    # Deterministic signal sanity tests; no market data or network required.
    f1, f2, f3 = protocol.FAMILIES
    if not engine.family_signal(f1, {
        "funding_z": -2.0, "funding": -0.001, "return_8h": -0.04,
        "return_4h": -0.02, "oi_change_8h": -0.05, "oi_change_4h": -0.02,
        "buy_ratio": 0.50, "bullish_signal_candle": False,
    }):
        problems.append("family 1 positive sanity signal failed")
    if not engine.family_signal(f2, {
        "funding_z": 0.0, "funding": 0.0, "return_8h": -0.04,
        "return_4h": -0.04, "oi_change_8h": -0.03, "oi_change_4h": -0.06,
        "buy_ratio": 0.50, "bullish_signal_candle": True,
    }):
        problems.append("family 2 positive sanity signal failed")
    if not engine.family_signal(f3, {
        "funding_z": 0.0, "funding": -0.0001, "return_8h": -0.03,
        "return_4h": -0.02, "oi_change_8h": 0.0, "oi_change_4h": 0.0,
        "buy_ratio": 0.40, "bullish_signal_candle": True,
    }):
        problems.append("family 3 positive sanity signal failed")

    print("=" * 88)
    print("TRADINGCORE HISTORICAL ACCELERATOR SELFTEST")
    print("Safety:", safety)
    print("Protocol:", protocol.PROTOCOL_VERSION, protocol.PROTOCOL_FINGERPRINT)
    print("Families:", ", ".join(str(x["id"]) for x in protocol.FAMILIES))
    print("Problems:", "NONE" if not problems else "; ".join(problems))
    print("REAL ORDER PATH: NOT PRESENT" if not problems else "SELFTEST FAILED")
    print("=" * 88)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
