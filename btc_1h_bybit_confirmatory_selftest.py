#!/usr/bin/env python3
from __future__ import annotations

import ast
import importlib
from pathlib import Path

from config.startup_safety import assert_safe_startup

FILES = ("btc_1h_bybit_confirmatory.py", "btc_1h_forward_shadow.py")
FORBIDDEN_TOKENS = ("create_order", "place_order", "submit_order", "api_key", "secret_key")
FORBIDDEN_IMPORTS = ("strategy_lab_orchestrator", "strategy_lab_deep_dive", "requests")


def imported_modules(tree: ast.AST) -> set[str]:
    """Return actual imported module names from Python syntax only.

    Comments/docstrings are intentionally ignored, avoiding false positives such
    as text saying that a module is *not* imported.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(str(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(str(node.module))
    return modules


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

        if name == "btc_1h_bybit_confirmatory.py":
            lower = text.lower()
            for token in FORBIDDEN_TOKENS:
                if token in lower:
                    problems.append(f"FORBIDDEN_TOKEN:{token}")

            actual_imports = imported_modules(tree)
            for forbidden in FORBIDDEN_IMPORTS:
                if any(
                    module == forbidden or module.startswith(forbidden + ".")
                    for module in actual_imports
                ):
                    problems.append(f"FORBIDDEN_IMPORT:{forbidden}")

    try:
        importlib.import_module("btc_1h_bybit_confirmatory")
    except Exception as exc:
        problems.append(f"IMPORT_SMOKE:{type(exc).__name__}:{exc}")

    smoke_failed = any(str(item).startswith("IMPORT_SMOKE:") for item in problems)

    print("=" * 84)
    print("BTC 1H BYBIT CONFIRMATORY SELFTEST V3")
    print("Safety:", safety)
    print("Problems:", problems or "NONE")
    print("Dependency smoke import:", "FAIL" if smoke_failed else "PASS")
    print("Import policy: AST / REAL IMPORTS ONLY")
    print("REAL ORDER PATH: NOT PRESENT")
    print("=" * 84)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
