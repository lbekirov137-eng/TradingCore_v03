from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_core.openai_provider import OpenAIProvider
from ai_core.provider_contracts import (
    AIDecision,
    AIMode,
    AIRequest,
)
from ai_observer.decision_filter import (
    FilterResult,
    evaluate_candidate,
    resolve_output_file,
    save_result,
)


class AIFilterAdapter:
    """
    Безопасный адаптер между PAPER-циклом и AI Decision Filter.

    Логика:
    1. Собирает единый payload из pipeline/context/snapshot.
    2. Всегда выполняет существующую локальную оценку.
    3. Для реального торгового кандидата BUY/SELL дополнительно
       запрашивает OpenAI в режиме SHADOW.
    4. Если OpenAI недоступен или ошибся, возвращает локальную оценку.
    5. Сохраняет результат в прежний JSONL-журнал.
    6. Никогда не открывает, не закрывает и не блокирует сделки.

    Гарантии безопасности:
    - только PAPER / SHADOW;
    - real_order_sent всегда False;
    - blocks_trade всегда False;
    - мнение AI не передаётся в execution path;
    - любая ошибка обрабатывается безопасно.
    """

    def __init__(
        self,
        output_file: Path | None = None,
        provider: OpenAIProvider | None = None,
    ) -> None:
        self.output_file = (
            output_file
            if output_file is not None
            else resolve_output_file()
        )

        self.provider = (
            provider
            if provider is not None
            else OpenAIProvider()
        )

        self.openai_shadow_enabled = (
            os.getenv(
                "AI_OPENAI_SHADOW_ENABLED",
                "true",
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

    @staticmethod
    def _safe_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value

        return {}

    @staticmethod
    def _safe_text(
        value: Any,
        default: str = "UNKNOWN",
    ) -> str:
        if value is None:
            return default

        text = str(value).strip()

        return text if text else default

    @staticmethod
    def _extract_context_market(
        context: Any,
    ) -> dict[str, Any]:
        market = getattr(context, "market", {})

        if not isinstance(market, dict):
            return {}

        return market

    @staticmethod
    def _extract_context_indicators(
        context: Any,
    ) -> dict[str, Any]:
        indicators = getattr(
            context,
            "indicators",
            {},
        )

        if not isinstance(indicators, dict):
            return {}

        return indicators

    @staticmethod
    def _extract_context_regime(
        context: Any,
    ) -> Any:
        for attribute_name in (
            "market_regime",
            "regime",
        ):
            value = getattr(
                context,
                attribute_name,
                None,
            )

            if value is not None:
                return value

        market = getattr(context, "market", {})

        if isinstance(market, dict):
            for key in (
                "market_regime",
                "regime",
            ):
                if market.get(key) is not None:
                    return market.get(key)

        return "UNKNOWN"

    def build_candidate_payload(
        self,
        *,
        pipeline_data: dict[str, Any],
        context: Any,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Собирает единый payload для локального фильтра и OpenAI.

        В payload входят:
        - решение торгового pipeline;
        - рыночный snapshot;
        - индикаторы;
        - режим рынка;
        - стратегия;
        - риск;
        - trade plan;
        - leverage/risk;
        - символ и таймфрейм.
        """

        pipeline = self._safe_dict(
            pipeline_data
        )

        market = self._extract_context_market(
            context
        )

        indicators = (
            self._extract_context_indicators(
                context
            )
        )

        decision = self._safe_dict(
            pipeline.get("decision")
        )

        trade_plan = self._safe_dict(
            pipeline.get("trade_plan")
        )

        risk = self._safe_dict(
            pipeline.get("risk")
        )

        paper_order = self._safe_dict(
            pipeline.get("paper_order")
        )

        strategy = self._safe_dict(
            pipeline.get("strategy")
        )

        leverage_risk = self._safe_dict(
            pipeline.get("leverage_risk")
        )

        symbol = getattr(
            context,
            "symbol",
            "BTCUSDT",
        )

        timeframe = getattr(
            context,
            "timeframe",
            "UNKNOWN",
        )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "market_price": snapshot.get(
                "price"
            ),
            "market_regime": (
                self._extract_context_regime(
                    context
                )
            ),
            "market": {
                **market,
                "price": snapshot.get(
                    "price"
                ),
                "candle_high": snapshot.get(
                    "candle_high"
                ),
                "candle_low": snapshot.get(
                    "candle_low"
                ),
                "last_close_time_ms": snapshot.get(
                    "close_time_ms"
                ),
            },
            "indicators": indicators,
            "strategy": strategy,
            "risk": risk,
            "trade_plan": trade_plan,
            "decision": decision,
            "paper_order": paper_order,
            "leverage_risk": leverage_risk,
            "real_orders_enabled": False,
            "shadow_only": True,
        }

        # Дублируем ключевые поля наверх для совместимости
        # с разными версиями pipeline и decision_filter.
        signal = (
            decision.get("signal")
            or strategy.get("signal")
            or paper_order.get("side")
        )

        if signal is not None:
            payload["signal"] = signal

        entry = (
            paper_order.get("entry")
            or trade_plan.get("entry")
            or trade_plan.get(
                "entry_price"
            )
        )

        if entry is not None:
            payload["entry"] = entry

        stop_loss = (
            paper_order.get("stop")
            or trade_plan.get(
                "stop_loss"
            )
            or trade_plan.get("sl")
        )

        if stop_loss is not None:
            payload["stop_loss"] = (
                stop_loss
            )

        take_profit = (
            paper_order.get(
                "take_profit_2"
            )
            or paper_order.get(
                "take_profit"
            )
            or trade_plan.get(
                "take_profit_2"
            )
            or trade_plan.get(
                "take_profit"
            )
            or trade_plan.get("tp2")
            or trade_plan.get("tp")
        )

        if take_profit is not None:
            payload["take_profit"] = (
                take_profit
            )

        return payload

    @staticmethod
    def _candidate_side(
        payload: dict[str, Any],
    ) -> str:
        value = (
            payload.get("signal")
            or AIFilterAdapter._safe_dict(
                payload.get("decision")
            ).get("side")
            or AIFilterAdapter._safe_dict(
                payload.get("strategy")
            ).get("side")
            or AIFilterAdapter._safe_dict(
                payload.get("paper_order")
            ).get("side")
        )

        side = str(value or "").strip().upper()

        aliases = {
            "LONG": "BUY",
            "BULLISH": "BUY",
            "UP": "BUY",
            "SHORT": "SELL",
            "BEARISH": "SELL",
            "DOWN": "SELL",
        }

        return aliases.get(side, side)

    @staticmethod
    def _confidence_label(
        confidence: float,
    ) -> str:
        if confidence >= 0.80:
            return "HIGH"

        if confidence >= 0.55:
            return "MEDIUM"

        return "LOW"

    @staticmethod
    def _filter_result_from_ai(
        *,
        ai_response: Any,
        local_result: FilterResult,
        source_file: Path,
    ) -> FilterResult:
        """
        Преобразует успешный AIResponse в старый формат FilterResult,
        чтобы journal и Telegram продолжили работать без изменений.
        """

        decision_value = getattr(
            ai_response.decision,
            "value",
            str(ai_response.decision),
        )

        return FilterResult(
            created_at_utc=local_result.created_at_utc,
            mode="SHADOW_OPENAI",
            decision=str(
                decision_value
            ).upper(),
            score=float(ai_response.score),
            confidence=(
                AIFilterAdapter._confidence_label(
                    float(ai_response.confidence)
                )
            ),
            symbol=local_result.symbol,
            side=local_result.side,
            market_regime=(
                local_result.market_regime
            ),
            trend=local_result.trend,
            structure=local_result.structure,
            rsi=local_result.rsi,
            atr=local_result.atr,
            risk_reward=(
                local_result.risk_reward
            ),
            source_signal_score=(
                local_result.source_signal_score
            ),
            reasons=[
                str(item)
                for item in ai_response.reasons
            ],
            warnings=[
                str(item)
                for item in ai_response.warnings
            ],
            real_order_sent=False,
            source_file=str(source_file),
        )

    def _run_openai_shadow(
        self,
        *,
        payload: dict[str, Any],
    ) -> Any:
        request = AIRequest(
            task_type="paper_trade_review",
            request_id=f"shadow-{uuid4()}",
            mode=AIMode.SHADOW,
            max_cost_usd=0.02,
            timeout_seconds=20.0,
            payload=payload,
            metadata={
                "source": "paper_live_loop.py",
                "adapter": "AIFilterAdapter",
                "purpose": (
                    "shadow_review_only"
                ),
                "must_not_affect_trading": True,
                "real_orders_enabled": False,
            },
        )

        return self.provider.analyze(request)

    def evaluate(
        self,
        *,
        pipeline_data: dict[str, Any],
        context: Any,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Выполняет двухуровневую SHADOW-оценку.

        Основной результат:
        - OpenAI, если есть настоящий BUY/SELL-кандидат
          и провайдер успешно ответил;
        - локальный FilterResult во всех остальных случаях.

        Торговый цикл никогда не должен падать из-за AI.
        """

        source_file = Path(
            "paper_live_loop.py"
        )

        try:
            payload = (
                self.build_candidate_payload(
                    pipeline_data=pipeline_data,
                    context=context,
                    snapshot=snapshot,
                )
            )

            # Локальная оценка выполняется всегда:
            # это резерв, baseline и источник совместимых полей.
            local_result = evaluate_candidate(
                data=payload,
                source_file=source_file,
            )

            selected_result = local_result
            provider_status = "LOCAL_ONLY"
            provider_name = "local_rules"
            provider_model: str | None = None
            provider_error_type: str | None = None
            provider_error: str | None = None
            openai_result_dict: dict[str, Any] | None = None

            side = self._candidate_side(payload)
            has_trade_candidate = (
                side in {"BUY", "SELL"}
            )

            should_call_openai = (
                self.openai_shadow_enabled
                and has_trade_candidate
                and self.provider.is_configured()
            )

            if should_call_openai:
                ai_response = (
                    self._run_openai_shadow(
                        payload=payload
                    )
                )

                openai_result_dict = (
                    ai_response.to_dict()
                )

                provider_name = (
                    ai_response.provider
                )
                provider_model = (
                    ai_response.model
                )

                if (
                    ai_response.success
                    and ai_response.decision
                    is not AIDecision.ERROR
                ):
                    selected_result = (
                        self._filter_result_from_ai(
                            ai_response=ai_response,
                            local_result=local_result,
                            source_file=source_file,
                        )
                    )
                    provider_status = (
                        "OPENAI_OK"
                    )

                else:
                    provider_status = (
                        "OPENAI_FAILED_SAFE_FALLBACK"
                    )
                    provider_error_type = (
                        ai_response.error_type
                    )
                    provider_error = (
                        ai_response.error_message
                    )

            elif not self.openai_shadow_enabled:
                provider_status = (
                    "OPENAI_DISABLED"
                )

            elif not has_trade_candidate:
                provider_status = (
                    "NO_TRADE_CANDIDATE"
                )

            elif not self.provider.is_configured():
                provider_status = (
                    "OPENAI_NOT_CONFIGURED"
                )

            # Сохраняем выбранный результат в прежнем формате.
            save_result(
                self.output_file,
                selected_result,
            )

            result_dict = asdict(
                selected_result
            )

            result_dict.update(
                {
                    "adapter_status": "OK",
                    "shadow_only": True,
                    "blocks_trade": False,
                    "can_open_trade": False,
                    "real_order_sent": False,
                    "provider_status": (
                        provider_status
                    ),
                    "provider": provider_name,
                    "model": provider_model,
                    "openai_shadow_enabled": (
                        self.openai_shadow_enabled
                    ),
                    "trade_candidate_detected": (
                        has_trade_candidate
                    ),
                    "local_baseline": asdict(
                        local_result
                    ),
                }
            )

            if openai_result_dict is not None:
                result_dict[
                    "openai_response"
                ] = openai_result_dict

            if provider_error_type:
                result_dict[
                    "provider_error_type"
                ] = provider_error_type

            if provider_error:
                result_dict[
                    "provider_error"
                ] = provider_error

            return result_dict

        except Exception as error:
            return {
                "adapter_status": (
                    "FAILED_SAFELY"
                ),
                "shadow_only": True,
                "blocks_trade": False,
                "can_open_trade": False,
                "decision": "NOT_AVAILABLE",
                "score": None,
                "confidence": "UNKNOWN",
                "provider_status": (
                    "ADAPTER_FAILED_SAFE"
                ),
                "error_type": (
                    type(error).__name__
                ),
                "error": str(error),
                "real_order_sent": False,
            }


def run_shadow_filter(
    *,
    pipeline_data: dict[str, Any],
    context: Any,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """
    Единая безопасная точка вызова из paper_live_loop.py.
    """

    adapter = AIFilterAdapter()

    return adapter.evaluate(
        pipeline_data=pipeline_data,
        context=context,
        snapshot=snapshot,
    )
