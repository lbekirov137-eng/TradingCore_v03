import math
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep
from api.signal_engine import SignalEngine


class StrategyStep(BaseStep):
    NAME = "Strategy Step"
    VERSION = "2.0.0"

    REQUIRED_INDICATORS = (
        "ema",
        "rsi",
        "structure",
    )

    ALLOWED_TRENDS = {
        "BULLISH",
        "BEARISH",
        "RANGE",
    }

    ALLOWED_STRUCTURES = {
        "UPTREND",
        "DOWNTREND",
        "RANGE",
        "UNKNOWN",
    }

    ALLOWED_SIGNALS = {
        "BUY",
        "SELL",
        "NO TRADE",
    }

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.indicators, dict):
            raise TypeError(
                "StrategyStep expected context.indicators to be dict"
            )

        for indicator_name in self.REQUIRED_INDICATORS:
            if indicator_name not in context.indicators:
                raise ValueError(
                    "StrategyStep missing indicator: "
                    f"{indicator_name}"
                )

            if not isinstance(
                context.indicators[indicator_name],
                dict,
            ):
                raise TypeError(
                    "StrategyStep indicator "
                    f"'{indicator_name}' must be dict"
                )

        ema = context.indicators["ema"]
        rsi = context.indicators["rsi"]
        structure = context.indicators["structure"]

        if "trend" not in ema:
            raise ValueError(
                "StrategyStep EMA trend is missing"
            )

        trend = ema["trend"]

        if trend not in self.ALLOWED_TRENDS:
            raise ValueError(
                f"StrategyStep invalid EMA trend: {trend}"
            )

        if "structure" not in structure:
            raise ValueError(
                "StrategyStep market structure is missing"
            )

        structure_value = structure["structure"]

        if structure_value not in self.ALLOWED_STRUCTURES:
            raise ValueError(
                "StrategyStep invalid market structure: "
                f"{structure_value}"
            )

        if "value" not in rsi:
            raise ValueError(
                "StrategyStep RSI value is missing"
            )

        rsi_value = rsi["value"]

        if not self._is_valid_number(rsi_value):
            raise ValueError(
                "StrategyStep RSI must be a finite number"
            )

        if not 0.0 <= float(rsi_value) <= 100.0:
            raise ValueError(
                "StrategyStep RSI must be between 0 and 100"
            )

    def process(self, context: MarketContext) -> MarketContext:
        ema = context.indicators["ema"]
        rsi = context.indicators["rsi"]
        structure = context.indicators["structure"]

        signal = SignalEngine.generate(
            trend=ema["trend"],
            structure=structure,
            rsi=rsi,
        )

        if not isinstance(signal, dict):
            raise TypeError(
                "SignalEngine.generate() must return dict"
            )

        signal_value = signal.get("signal")

        if signal_value not in self.ALLOWED_SIGNALS:
            raise ValueError(
                "SignalEngine returned invalid signal: "
                f"{signal_value}"
            )

        context.strategy = signal

        context.audit["strategy_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "signal": signal_value,
        }

        return context

    @staticmethod
    def _is_valid_number(value: Any) -> bool:
        if isinstance(value, bool):
            return False

        if not isinstance(value, (int, float)):
            return False

        return math.isfinite(float(value))