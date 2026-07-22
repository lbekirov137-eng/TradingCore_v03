from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvidenceResult:

    approved: bool = False

    score: float = 0.0

    confidence: float = 0.0

    stage: str = ""

    message: str = ""

    details: dict[str, Any] = field(default_factory=dict)