from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class MarketIntelligenceResult:
    primary_regime: str
    trend: str
    structure: str
    volatility: str
    confidence: float
    data_quality: str
    strategy_allowed: bool
    reasons: list[str]
    warnings: list[str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketIntelligenceEngine:
    """
    Deterministic Market Intelligence Engine v1.

    Purpose:
    - classify market regime from existing indicators and candles;
    - write a stable, explainable result for downstream strategy,
      risk, journal and OpenAI SHADOW review;
    - never open, close or block a trade directly.

    Expected inputs:
    - market: highs, lows, closes
    - indicators: ema, rsi, atr, structure
    """

    VERSION = "1.0.0"

    MIN_CANDLES = 50

    @staticmethod
    def _safe_float(
        value: Any,
        default: float | None = None,
    ) -> float | None:
        try:
            if value is None or isinstance(value, bool):
                return default

            result = float(value)

            if not math.isfinite(result):
                return default

            return result

        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _last_valid(
        values: Any,
    ) -> float | None:
        if not isinstance(values, (list, tuple)):
            return None

        for value in reversed(values):
            parsed = MarketIntelligenceEngine._safe_float(value)

            if parsed is not None:
                return parsed

        return None

    @staticmethod
    def _data_quality(
        *,
        highs: Any,
        lows: Any,
        closes: Any,
        ema: dict[str, Any],
        rsi: dict[str, Any],
        atr: dict[str, Any],
        structure: dict[str, Any],
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []

        if not all(
            isinstance(values, (list, tuple))
            for values in (highs, lows, closes)
        ):
            return "INVALID", [
                "Market candles are missing or not list/tuple values."
            ]

        if not (
            len(highs)
            == len(lows)
            == len(closes)
        ):
            return "INVALID", [
                "High, low and close series have unequal lengths."
            ]

        if len(closes) < MarketIntelligenceEngine.MIN_CANDLES:
            warnings.append(
                "Insufficient candle history for robust regime analysis."
            )

        required_indicator_sets = {
            "ema": ema,
            "rsi": rsi,
            "atr": atr,
            "structure": structure,
        }

        missing = [
            name
            for name, value in required_indicator_sets.items()
            if not value
        ]

        if missing:
            warnings.append(
                "Missing indicator groups: "
                + ", ".join(missing)
            )

        if warnings:
            return "LIMITED", warnings

        return "GOOD", warnings

    @staticmethod
    def analyze(
        *,
        market: dict[str, Any],
        indicators: dict[str, Any],
    ) -> MarketIntelligenceResult:
        market = MarketIntelligenceEngine._safe_dict(market)
        indicators = MarketIntelligenceEngine._safe_dict(indicators)

        highs = market.get("highs", [])
        lows = market.get("lows", [])
        closes = market.get("closes", [])

        ema = MarketIntelligenceEngine._safe_dict(
            indicators.get("ema")
        )
        rsi = MarketIntelligenceEngine._safe_dict(
            indicators.get("rsi")
        )
        atr = MarketIntelligenceEngine._safe_dict(
            indicators.get("atr")
        )
        structure_data = MarketIntelligenceEngine._safe_dict(
            indicators.get("structure")
        )

        data_quality, quality_warnings = (
            MarketIntelligenceEngine._data_quality(
                highs=highs,
                lows=lows,
                closes=closes,
                ema=ema,
                rsi=rsi,
                atr=atr,
                structure=structure_data,
            )
        )

        reasons: list[str] = []
        warnings = list(quality_warnings)

        trend = str(
            ema.get("trend", "UNKNOWN")
        ).strip().upper()

        structure = str(
            structure_data.get(
                "structure",
                "UNKNOWN",
            )
        ).strip().upper()

        rsi_value = MarketIntelligenceEngine._safe_float(
            rsi.get("value")
        )
        atr_value = MarketIntelligenceEngine._safe_float(
            atr.get("value")
        )
        last_price = (
            MarketIntelligenceEngine._last_valid(closes)
        )

        atr_percent: float | None = None

        if (
            atr_value is not None
            and last_price is not None
            and last_price > 0
        ):
            atr_percent = (
                atr_value / last_price
            ) * 100.0

        if atr_percent is None:
            volatility = "UNKNOWN"
            warnings.append(
                "ATR percentage could not be calculated."
            )
        elif atr_percent >= 1.20:
            volatility = "HIGH"
            reasons.append(
                f"ATR is {atr_percent:.3f}% of price: high volatility."
            )
        elif atr_percent <= 0.25:
            volatility = "LOW"
            reasons.append(
                f"ATR is {atr_percent:.3f}% of price: low volatility."
            )
        else:
            volatility = "NORMAL"
            reasons.append(
                f"ATR is {atr_percent:.3f}% of price: normal volatility."
            )

        bullish_alignment = (
            trend == "BULLISH"
            and structure == "UPTREND"
        )
        bearish_alignment = (
            trend == "BEARISH"
            and structure == "DOWNTREND"
        )
        range_alignment = (
            trend == "RANGE"
            or structure == "RANGE"
        )

        if bullish_alignment:
            primary_regime = "TREND_UP"
            reasons.append(
                "EMA trend and market structure are bullish and aligned."
            )
        elif bearish_alignment:
            primary_regime = "TREND_DOWN"
            reasons.append(
                "EMA trend and market structure are bearish and aligned."
            )
        elif range_alignment:
            primary_regime = "RANGE"
            reasons.append(
                "Trend or structure indicates a range-bound market."
            )
        else:
            primary_regime = "TRANSITION"
            warnings.append(
                "Trend and structure are not aligned."
            )

        score = 40.0

        if data_quality == "GOOD":
            score += 20.0
        elif data_quality == "LIMITED":
            score += 5.0
        else:
            score = 0.0

        if bullish_alignment or bearish_alignment:
            score += 25.0
        elif primary_regime == "RANGE":
            score += 15.0
        elif primary_regime == "TRANSITION":
            score += 5.0

        if volatility == "NORMAL":
            score += 10.0
        elif volatility in {"HIGH", "LOW"}:
            score += 3.0

        if rsi_value is not None:
            if 35.0 <= rsi_value <= 65.0:
                score += 5.0
            elif rsi_value >= 75.0 or rsi_value <= 25.0:
                warnings.append(
                    "RSI is at an extreme level."
                )

        confidence = max(
            0.0,
            min(100.0, round(score, 2)),
        )

        strategy_allowed = (
            data_quality != "INVALID"
            and primary_regime
            in {
                "TREND_UP",
                "TREND_DOWN",
                "RANGE",
            }
            and confidence >= 55.0
            and volatility != "UNKNOWN"
        )

        if not strategy_allowed:
            warnings.append(
                "Market Intelligence does not currently support "
                "a high-confidence strategy search."
            )

        metrics = {
            "last_price": (
                round(last_price, 8)
                if last_price is not None
                else None
            ),
            "atr": (
                round(atr_value, 8)
                if atr_value is not None
                else None
            ),
            "atr_percent": (
                round(atr_percent, 6)
                if atr_percent is not None
                else None
            ),
            "rsi": (
                round(rsi_value, 4)
                if rsi_value is not None
                else None
            ),
            "ema20": ema.get("ema20"),
            "ema50": ema.get("ema50"),
            "ema200": ema.get("ema200"),
            "candles_count": (
                len(closes)
                if isinstance(closes, (list, tuple))
                else 0
            ),
        }

        return MarketIntelligenceResult(
            primary_regime=primary_regime,
            trend=trend,
            structure=structure,
            volatility=volatility,
            confidence=confidence,
            data_quality=data_quality,
            strategy_allowed=strategy_allowed,
            reasons=reasons,
            warnings=warnings,
            metrics=metrics,
        )
