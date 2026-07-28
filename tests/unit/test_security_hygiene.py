"""
Operationalizes the security review as a regression check so future
commits don't silently reintroduce secrets or dangerous constructs.
Deliberately conservative: only flags obvious, high-confidence patterns.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_DIRS = ["api", "config"]

SECRET_PATTERN = re.compile(
    r"""(api_key|apikey|secret|token|password)\s*=\s*["'][^"'\s]{8,}["']""",
    re.IGNORECASE,
)

DANGEROUS_CALLS = re.compile(r"\b(eval|exec)\s*\(|pickle\.load|os\.system|subprocess\.")


def _py_files():
    for d in SCAN_DIRS:
        yield from (REPO_ROOT / d).rglob("*.py")


def test_no_hardcoded_secret_like_assignments():
    offenders = []

    for path in _py_files():
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in SECRET_PATTERN.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")

    assert offenders == [], f"Possible hardcoded secret-like assignment(s): {offenders}"


def test_no_dangerous_dynamic_execution_or_shell_calls():
    offenders = []

    for path in _py_files():
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in DANGEROUS_CALLS.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")

    assert offenders == [], f"Dangerous dynamic execution / shell usage found: {offenders}"


def test_no_env_file_committed_or_present():
    env_like = list(REPO_ROOT.glob(".env*"))
    # .env.example / .env.sample are fine (no secret values); a bare .env is not.
    disallowed = [p for p in env_like if p.name == ".env"]
    assert disallowed == [], f"A real .env file is present in the repo root: {disallowed}"


def test_env_example_contains_no_real_looking_secret_values():
    """
    .env.example must document required variables without ever containing
    a plausible real secret value (only empty assignments or safe defaults).
    """
    env_example = REPO_ROOT / ".env.example"
    assert env_example.exists(), ".env.example must exist"

    text = env_example.read_text(encoding="utf-8")

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if any(marker in key.upper() for marker in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            assert value.strip() == "", f".env.example line has a non-empty secret-like value: {line}"


def test_no_order_placement_endpoints_exist_in_exchange_clients():
    """
    Structural proof that no live-order code path exists: neither Binance
    nor Bybit REST clients define any order-creation method. This is the
    real reason live trading is impossible, independent of any config flag.
    """
    import inspect
    from api import binance, bybit

    order_like_names = ("create_order", "place_order", "new_order", "submit_order", "cancel_order")

    offenders = []
    for module in (binance.BinanceAPI, bybit.BybitAPI):
        methods = {name for name, _ in inspect.getmembers(module, predicate=inspect.isfunction)}
        for forbidden in order_like_names:
            if forbidden in methods:
                offenders.append(f"{module.__name__}.{forbidden}")

    assert offenders == [], f"Order-placement-like methods found on public exchange clients: {offenders}"


def test_live_trading_cannot_be_enabled_by_any_single_env_var_except_the_explicit_one():
    """
    Only LIVE_TRADING=true (or TRADING_ENVIRONMENT=LIVE) can trigger the
    startup refusal path -- no other plausible env var name/typo silently
    enables anything resembling live trading.
    """
    from config.startup_safety import get_live_trading_flag
    import os

    suspicious_names = [
        "TRADING_MODE", "MODE", "ENV", "ENVIRONMENT", "REAL_TRADING",
        "PRODUCTION", "IS_LIVE", "MAINNET",
    ]

    for name in suspicious_names:
        os.environ[name] = "true"
        try:
            assert get_live_trading_flag() is False, f"{name}=true unexpectedly affected live-trading flag"
        finally:
            del os.environ[name]
