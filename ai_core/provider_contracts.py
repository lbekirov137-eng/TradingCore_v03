from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol


class AIMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ADVISOR = "ADVISOR"
    GUARD = "GUARD"


class AIDecision(str, Enum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"
    BLOCK = "BLOCK"
    NO_DECISION = "NO_DECISION"
    ERROR = "ERROR"


@dataclass(frozen=True)
class AIRequest:
    task_type: str
    payload: dict[str, Any]
    request_id: str
    mode: AIMode = AIMode.SHADOW
    preferred_provider: str | None = None
    preferred_model: str | None = None
    max_cost_usd: float = 0.02
    timeout_seconds: float = 20.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["mode"] = self.mode.value
        return result


@dataclass(frozen=True)
class AIResponse:
    request_id: str
    provider: str
    model: str
    decision: AIDecision
    score: float
    confidence: float
    summary: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_output: str | None = None
    latency_ms: int | None = None
    estimated_cost_usd: float | None = None
    created_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    success: bool = True
    error_type: str | None = None
    error_message: str | None = None
    blocks_trade: bool = False
    can_open_trade: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", max(0.0, min(100.0, float(self.score))))
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        object.__setattr__(self, "can_open_trade", False)
        object.__setattr__(
            self,
            "blocks_trade",
            self.decision == AIDecision.BLOCK,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["decision"] = self.decision.value
        return result


class AIProvider(Protocol):
    @property
    def provider_name(self) -> str:
        ...

    def is_configured(self) -> bool:
        ...

    def analyze(self, request: AIRequest) -> AIResponse:
        ...


def build_safe_error_response(
    *,
    request: AIRequest,
    provider: str,
    model: str,
    error: Exception,
) -> AIResponse:
    return AIResponse(
        request_id=request.request_id,
        provider=provider,
        model=model,
        decision=AIDecision.ERROR,
        score=0.0,
        confidence=0.0,
        summary=(
            "AI provider failed safely. "
            "Trading decision was not delegated to AI."
        ),
        warnings=[
            "AI response unavailable.",
            "Real orders remain disabled.",
        ],
        success=False,
        error_type=type(error).__name__,
        error_message=str(error),
    )
