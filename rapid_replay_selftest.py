#!/usr/bin/env python3
from __future__ import annotations

import ast
import importlib
from pathlib import Path

from config.startup_safety import assert_safe_startup
from rapid_replay_protocol import HISTORY_DAYS, PROTOCOL_FINGERPRINT, PROTOCOL_VERSION, VENUES

FILES = ("rapid_replay.py", "rapid_replay_protocol.py")
FORBIDDEN_CALLS = ("create_order", "place_order", "submit_order", "cancel_order")
FORBIDDEN_IMPORT_ROOTS = ("ccxt",)


def imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def main() -> int:
    safety = assert_safe_startup()
    root = Path(__file__).resolve().parent
    problems: list[str] = []
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
        if name == "rapid_replay.py":
            lower = text.lower()
            for token in FORBIDDEN_CALLS:
                if token in lower:
                    problems.append(f"FORBIDDEN_ORDER_TOKEN:{token}")
            actual = imports(tree)
            for root_name in FORBIDDEN_IMPORT_ROOTS:
                if any(x == root_name or x.startswith(root_name + ".") for x in actual):
                    problems.append(f"FORBIDDEN_IMPORT:{root_name}")
    try:
        importlib.import_module("rapid_replay")
    except Exception as exc:
        problems.append(f"IMPORT_SMOKE:{type(exc).__name__}:{exc}")
    if HISTORY_DAYS != 365:
        problems.append(f"HISTORY_DAYS_CHANGED:{HISTORY_DAYS}")
    if tuple(VENUES) != ("BINANCE", "BYBIT"):
        problems.append(f"VENUES_CHANGED:{VENUES}")
    print("=" * 92)
    print("TRADINGCORE RAPID REPLAY SELFTEST")
    print("Safety:", safety)
    print("Protocol:", PROTOCOL_VERSION, PROTOCOL_FINGERPRINT)
    print("History days:", HISTORY_DAYS, "Venues:", VENUES)
    print("Problems:", problems or "NONE")
    print("REAL ORDER PATH: NOT PRESENT")
    print("=" * 92)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
