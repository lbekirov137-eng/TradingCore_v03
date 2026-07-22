from __future__ import annotations

from api.contracts.context import MarketContext
from api.market_intelligence_engine import (
    MarketIntelligenceEngine,
)
from api.pipeline_v2.steps.base_step import BaseStep


class MarketIntelligenceStep(BaseStep):
    NAME = "Market Intelligence Step"
    VERSION = "1.0.0"

    def validate(
        self,
        context: MarketContext,
    ) -> None:
        super().validate(context)

        if not isinstance(context.market, dict):
            raise TypeError(
                "MarketIntelligenceStep expected "
                "context.market to be dict"
            )

        if not isinstance(context.indicators, dict):
            raise TypeError(
                "MarketIntelligenceStep expected "
                "context.indicators to be dict"
            )

        for indicator_name in (
            "ema",
            "rsi",
            "atr",
            "structure",
        ):
            if indicator_name not in context.indicators:
                raise ValueError(
                    "MarketIntelligenceStep missing indicator: "
                    f"{indicator_name}"
                )

    def process(
        self,
        context: MarketContext,
    ) -> MarketContext:
        result = MarketIntelligenceEngine.analyze(
            market=context.market,
            indicators=context.indicators,
        )

        context.regime = result.to_dict()

        context.audit[
            "market_intelligence_step"
        ] = {
            "status": "OK",
            "version": self.VERSION,
            "primary_regime": (
                result.primary_regime
            ),
            "confidence": result.confidence,
            "strategy_allowed": (
                result.strategy_allowed
            ),
        }

        return context
