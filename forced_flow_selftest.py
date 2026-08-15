#!/usr/bin/env python3
"""Static/runtime safety self-test for the autonomous forced-flow stack."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

from config.startup_safety import assert_safe_startup

FILES = (
    "forced_flow_protocol.py",
    "forced_flow_research_engine.py",
    "forced_flow_autonomous_orchestrator.py",
    "forced_flow_forward_paper.py",
)

BANNED_CALL_NAMES = {
    "create_order",
    "place_order",
    "submit_order",
    "send_order",
    "cancel_order",
    "amend_order",
}
BANNED_IMPORT_FRAGMENTS = {
    "pybit",
    "ccxt",
    "binance.client",
    "bybit_api",
}


def main() -> int:
    safety = assert_safe_startup()
    root = Path(__file__).resolve().parent
    problems: list[str] = []

    for name in FILES:
        path = root / name
        if not path.exists():
            problems.append(f"missing:{name}")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=name)
        except SyntaxError as error:
            problems.append(f"syntax:{name}:{error}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    modules = [node.module or ""]
                for module in modules:
                    lowered = module.lower()
                    if any(fragment in lowered for fragment in BANNED_IMPORT_FRAGMENTS):
                        problems.append(f"banned_import:{name}:{module}")
            if isinstance(node, ast.Call):
                func = node.func
                call_name = None
                if isinstance(func, ast.Name):
                    call_name = func.id
                elif isinstance(func, ast.Attribute):
                    call_name = func.attr
                if call_name and call_name.lower() in BANNED_CALL_NAMES:
                    problems.append(f"banned_order_call:{name}:{call_name}")

    protocol = importlib.import_module("forced_flow_protocol")
    if getattr(protocol, "MAX_LEVERAGE", None) != 1.0:
        problems.append("protocol:max_leverage_not_1x")
    if getattr(protocol, "RISK_AMOUNT_USD", None) != 1.0:
        problems.append("protocol:risk_amount_changed")
    if not getattr(protocol, "PROTOCOL_FINGERPRINT", ""):
        problems.append("protocol:fingerprint_missing")

    for module in (
        "forced_flow_research_engine",
        "forced_flow_autonomous_orchestrator",
        "forced_flow_forward_paper",
    ):
        importlib.import_module(module)

    print("=" * 76)
    print("TRADINGCORE FORCED-FLOW AUTONOMOUS STACK SELFTEST")
    print("Safety:", safety)
    print("Protocol:", protocol.PROTOCOL_VERSION, protocol.PROTOCOL_FINGERPRINT)
    print("Problems:", problems if problems else "NONE")
    print("REAL ORDER PATH: NOT PRESENT" if not problems else "SELFTEST FAILED")
    print("=" * 76)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
