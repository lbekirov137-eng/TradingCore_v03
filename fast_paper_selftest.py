#!/usr/bin/env python3
from __future__ import annotations

import ast
import importlib
from pathlib import Path

from config.startup_safety import assert_safe_startup
import fast_paper_protocol as protocol

FILES = ("fast_paper_protocol.py", "fast_paper_strategies.py", "fast_paper_cloud_once.py")
FORBIDDEN_CALL_TOKENS = ("create_order", "place_order", "submit_order", "private_api", "api_key", "secret_key")
FORBIDDEN_IMPORT_ROOTS = ("ccxt", "binance.client", "pybit.unified_trading")


def actual_imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(str(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(str(node.module))
    return modules


def main() -> int:
    safety = assert_safe_startup()
    root = Path(__file__).resolve().parent
    problems: list[str] = []

    expected_lanes = len(protocol.SYMBOLS) * len(protocol.TIMEFRAMES) * len(protocol.HYPOTHESES)
    if expected_lanes != 36:
        problems.append(f"LANE_COUNT:{expected_lanes}")
    if protocol.MIN_FORWARD_CLOSED_TRADES != 30:
        problems.append(f"FIRST_DECISION_TARGET:{protocol.MIN_FORWARD_CLOSED_TRADES}")
    if protocol.MAX_LEVERAGE != 1.0:
        problems.append(f"LEVERAGE:{protocol.MAX_LEVERAGE}")

    for name in FILES:
        path = root / name
        if not path.exists():
            problems.append(f"MISSING:{name}")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=name)
        except SyntaxError as exc:
            problems.append(f"SYNTAX:{name}:{exc}")
            continue
        imports = actual_imports(tree)
        for forbidden in FORBIDDEN_IMPORT_ROOTS:
            if any(module == forbidden or module.startswith(forbidden + ".") for module in imports):
                problems.append(f"FORBIDDEN_IMPORT:{name}:{forbidden}")
        if name == "fast_paper_cloud_once.py":
            lowered = text.lower()
            for token in FORBIDDEN_CALL_TOKENS:
                if token in lowered and token not in ("private_api",):
                    # Marker fields such as private_api_used are allowed; order/API credential paths are not.
                    if token in ("api_key", "secret_key") or f"{token}(" in lowered:
                        problems.append(f"FORBIDDEN_TOKEN:{name}:{token}")

    try:
        importlib.import_module("fast_paper_cloud_once")
    except Exception as exc:
        problems.append(f"IMPORT_SMOKE:{type(exc).__name__}:{exc}")

    print("=" * 88)
    print("TRADINGCORE FAST PAPER SELFTEST")
    print("Safety:", safety)
    print("Protocol:", protocol.PROTOCOL_VERSION, protocol.PROTOCOL_FINGERPRINT)
    print("Frozen lanes:", expected_lanes)
    print("First lane decision: FIRST 30 CLOSED FORWARD TRADES")
    print("Problems:", problems or "NONE")
    print("REAL ORDER PATH: NOT PRESENT")
    print("=" * 88)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
