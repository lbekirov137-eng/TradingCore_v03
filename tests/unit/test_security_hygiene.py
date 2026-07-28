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
