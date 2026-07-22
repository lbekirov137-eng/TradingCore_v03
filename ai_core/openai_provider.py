from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI

from ai_core.provider_contracts import (
    AIDecision,
    AIRequest,
    AIResponse,
    build_safe_error_response,
)


class OpenAIProvider:
    """
    OpenAI provider for TradingCore.

    Safety guarantees:
    - The API key is read only from OPENAI_API_KEY.
    - AI cannot create or open a trade.
    - SHADOW/ADVISOR output does not control execution.
    - Any API, timeout, empty-output, or parsing failure is converted
      into a safe AIResponse.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
    ) -> None:
        self.model = (
            model
            or os.getenv(
                "OPENAI_MODEL",
                "gpt-5-mini",
            ).strip()
        )

        self._api_key = os.getenv(
            "OPENAI_API_KEY",
            "",
        ).strip()

        self._client = (
            OpenAI(api_key=self._api_key)
            if self._api_key
            else None
        )

    @property
    def provider_name(self) -> str:
        return "openai"

    def is_configured(self) -> bool:
        return bool(
            self._client
            and self._api_key
        )

    @staticmethod
    def _instructions() -> str:
        return """
You are an independent risk reviewer for a PAPER-only crypto trading system.

Evaluate only the candidate supplied by the deterministic trading system.

Mandatory rules:
1. Never create, suggest, or open a new trade.
2. Never override deterministic risk controls.
3. Unknown, missing, contradictory, or low-quality data must reduce the score.
4. Market regime UNKNOWN must never receive APPROVE.
5. Prefer NO TRADE over weak evidence.
6. REJECT means weak evidence without a hard safety failure.
7. BLOCK means a hard safety, consistency, or data-quality failure.
8. Keep the explanation short and factual.
""".strip()

    @staticmethod
    def _response_format() -> dict[str, Any]:
        return {
            "type": "json_schema",
            "name": "trading_risk_review",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": [
                            "APPROVE",
                            "REVIEW",
                            "REJECT",
                            "BLOCK",
                        ],
                    },
                    "score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "summary": {
                        "type": "string",
                    },
                    "reasons": {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                    },
                    "warnings": {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                    },
                },
                "required": [
                    "decision",
                    "score",
                    "confidence",
                    "summary",
                    "reasons",
                    "warnings",
                ],
                "additionalProperties": False,
            },
        }

    @staticmethod
    def _build_input(
        request: AIRequest,
    ) -> str:
        safe_payload = {
            "task_type": request.task_type,
            "request_id": request.request_id,
            "mode": request.mode.value,
            "payload": request.payload,
            "metadata": request.metadata,
        }

        return json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _parse_json(
        raw_text: str,
    ) -> dict[str, Any]:
        text = raw_text.strip()

        if not text:
            raise ValueError(
                "OpenAI returned an empty output_text"
            )

        parsed = json.loads(text)

        if not isinstance(parsed, dict):
            raise ValueError(
                "OpenAI response is not a JSON object"
            )

        return parsed

    @staticmethod
    def _decision_from_text(
        value: Any,
    ) -> AIDecision:
        normalized = str(value).strip().upper()

        mapping = {
            "APPROVE": AIDecision.APPROVE,
            "REVIEW": AIDecision.REVIEW,
            "REJECT": AIDecision.REJECT,
            "BLOCK": AIDecision.BLOCK,
        }

        return mapping.get(
            normalized,
            AIDecision.ERROR,
        )

    def analyze(
        self,
        request: AIRequest,
    ) -> AIResponse:
        selected_model = (
            request.preferred_model
            or self.model
        )

        if not self.is_configured():
            return AIResponse(
                request_id=request.request_id,
                provider=self.provider_name,
                model=selected_model,
                decision=AIDecision.ERROR,
                score=0.0,
                confidence=0.0,
                summary=(
                    "OpenAI provider is not configured."
                ),
                warnings=[
                    "OPENAI_API_KEY is missing.",
                    "Trading decision was not delegated to AI.",
                ],
                success=False,
                error_type="ConfigurationError",
                error_message=(
                    "OPENAI_API_KEY is not available"
                ),
            )

        started = time.perf_counter()

        try:
            response = self._client.responses.create(
                model=selected_model,
                instructions=self._instructions(),
                input=self._build_input(request),
                reasoning={
                    "effort": "minimal",
                },
                text={
                    "format": self._response_format(),
                    "verbosity": "low",
                },
                max_output_tokens=1200,
            )

            if response.status != "completed":
                raise RuntimeError(
                    "OpenAI response did not complete: "
                    f"status={response.status}, "
                    f"incomplete_details={response.incomplete_details}"
                )

            raw_text = (
                response.output_text
                or ""
            ).strip()

            parsed = self._parse_json(raw_text)

            decision = self._decision_from_text(
                parsed.get("decision")
            )

            if decision is AIDecision.ERROR:
                raise ValueError(
                    "OpenAI returned an unsupported decision"
                )

            latency_ms = int(
                (
                    time.perf_counter()
                    - started
                )
                * 1000
            )

            return AIResponse(
                request_id=request.request_id,
                provider=self.provider_name,
                model=selected_model,
                decision=decision,
                score=float(
                    parsed.get("score", 0.0)
                ),
                confidence=float(
                    parsed.get("confidence", 0.0)
                ),
                summary=str(
                    parsed.get("summary", "")
                ),
                reasons=[
                    str(item)
                    for item in parsed.get(
                        "reasons",
                        [],
                    )
                ],
                warnings=[
                    str(item)
                    for item in parsed.get(
                        "warnings",
                        [],
                    )
                ],
                raw_output=raw_text,
                latency_ms=latency_ms,
                estimated_cost_usd=None,
                success=True,
            )

        except Exception as error:
            return build_safe_error_response(
                request=request,
                provider=self.provider_name,
                model=selected_model,
                error=error,
            )
