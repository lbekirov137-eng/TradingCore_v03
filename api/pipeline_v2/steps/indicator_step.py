import math
from typing import Any

from api.atr import ATREngine
from api.contracts.context import MarketContext
from api.ema import EMAEngine
from api.market_structure import MarketStructure
from api.pipeline_v2.steps.base_step import BaseStep
from api.rsi import RSIEngine


class IndicatorStep(BaseStep):
    NAME = "Indicator Step"
    VERSION = "2.0.0"

    MIN_CANDLES = 200
    REQUIRED_MARKET_FIELDS = (
        "highs",
        "lows",
        "closes",
    )

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.market, dict):
            raise TypeError("IndicatorStep expected context.market to be dict")

        for field_name in self.REQUIRED_MARKET_FIELDS:
            if field_name not in context.market:
                raise ValueError(
                    f"IndicatorStep missing market field: {field_name}"
                )

            values = context.market[field_name]

            if not isinstance(values, (list, tuple)):
                raise TypeError(
                    f"IndicatorStep market field '{field_name}' "
                    "must be a list or tuple"
                )

            if len(values) < self.MIN_CANDLES:
                raise ValueError(
                    f"IndicatorStep requires at least "
                    f"{self.MIN_CANDLES} candles"
                )

            for value in values:
                if not self._is_valid_number(value):
                    raise ValueError(
                        f"IndicatorStep market field '{field_name}' "
                        "contains invalid numeric data"
                    )

        highs = context.market["highs"]
        lows = context.market["lows"]
        closes = context.market["closes"]

        if not (
            len(highs)
            == len(lows)
            == len(closes)
        ):
            raise ValueError(
                "IndicatorStep highs, lows and closes "
                "must have equal lengths"
            )

        for high, low, close in zip(highs, lows, closes):
            if high < low:
                raise ValueError(
                    "IndicatorStep candle high cannot be below low"
                )

            if close < low or close > high:
                raise ValueError(
                    "IndicatorStep candle close must be "
                    "between low and high"
                )

    def process(self, context: MarketContext) -> MarketContext:
        closes = context.market["closes"]
        highs = context.market["highs"]
        lows = context.market["lows"]

        context.indicators["ema"] = EMAEngine.calculate_all(
            closes
        )

        context.indicators["rsi"] = RSIEngine.calculate(
            closes
        )

        context.indicators["atr"] = ATREngine.calculate(
            highs,
            lows,
            closes,
        )

        context.indicators["structure"] = (
            MarketStructure.analyze(
                highs,
                lows,
            )
        )

        context.audit["indicator_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "candles_processed": len(closes),
        }

        return context

    @staticmethod
    def _is_valid_number(value: Any) -> bool:
        if isinstance(value, bool):
            return False

        if not isinstance(value, (int, float)):
            return False

        return math.isfinite(float(value))