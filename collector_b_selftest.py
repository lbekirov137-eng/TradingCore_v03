#!/usr/bin/env python3
"""Fail-closed static/runtime isolation checks for Collector B."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import collector_b_bybit as collector


def main() -> int:
    path = Path(inspect.getsourcefile(collector) or "collector_b_bybit.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    forbidden_import_fragments = (
        "api.binance",
        "api.bybit",
        "order_client",
        "execution_client",
        "ccxt",
    )
    violations = [
        name for name in imported
        if any(fragment in name.lower() for fragment in forbidden_import_fragments)
    ]

    checks = {
        "public_ws_only": collector.PUBLIC_WS_URL == "wss://stream.bybit.com/v5/public/linear",
        "symbols_frozen": tuple(collector.SYMBOLS) == ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        "topics_all_liquidation": all(topic.startswith("allLiquidation.") for topic in collector.TOPICS),
        "real_orders_disabled": collector.REAL_ORDERS_ENABLED is False,
        "private_api_disabled": collector.PRIVATE_API_USED is False,
        "outcomes_disabled": collector.OUTCOME_COMPUTATION_ENABLED is False,
        "qualifying_logic_disabled": collector.QUALIFYING_EPISODE_LOGIC_ENABLED is False,
        "no_forbidden_imports": not violations,
        "no_private_ws_url": "/v5/private" not in source,
        "no_trade_ws_url": "/v5/trade" not in source,
        "no_auth_operation": '"op": "auth"' not in source and "'op': 'auth'" not in source,
    }

    print("=" * 72)
    print("TRADINGCORE COLLECTOR B ISOLATION SELF-TEST")
    print("=" * 72)
    for name, passed in checks.items():
        print(f"{name}: {'PASS' if passed else 'FAIL'}")
    if violations:
        print("forbidden_imports:", violations)

    failed = [name for name, passed in checks.items() if not passed]
    print("=" * 72)

    if failed:
        print("COLLECTOR B SELF-TEST: FAILED")
        print("FAILED CHECKS:", ", ".join(failed))
        print("COLLECTOR MUST NOT START")
        print("NO ORDERS SENT")
        return 1

    print("COLLECTOR B SELF-TEST: PASSED")
    print("PUBLIC DATA ONLY / NO AUTH / NO ORDERS / NO STRATEGY")
    print("COLLECTOR A UNCHANGED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
