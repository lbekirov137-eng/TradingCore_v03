from dataclasses import dataclass


@dataclass
class RuleResult:

    passed: bool

    critical: bool = False

    score: int = 0

    confidence: float = 0.0

    direction: str | None = None

    reason: str = ""