from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_JOURNAL_FILE = Path("data/ai_opportunity_review.jsonl")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    if not math.isfinite(number):
        return default

    return number


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _normalize_ratio(value: Any, default: float = 0.0) -> float:
    """
    Converts common confidence/quality formats to 0.0..1.0.

    Accepted examples:
    - 0.75 -> 0.75
    - 75   -> 0.75
    """
    number = _safe_float(value, default)

    if number > 1.0:
        number = number / 100.0

    return _clamp(number, 0.0, 1.0)


def _first_present(
    sources: list[dict[str, Any]],
    keys: tuple[str, ...],
    default: Any = None,
) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value

    return default


@dataclass(frozen=True)
class OpportunityReview:
    recorded_at_utc: str
    symbol: str
    price: float
    candle_high: float
    candle_low: float
    strategy_decision: str
    opportunity_decision: str
    score: float
    confidence: float
    reasons: list[str]
    market_regime: str
    trend: str
    trend_strength: float
    signal_quality: float
    volatility: float
    volatility_label: str
    liquidity: float
    atr: float
    atr_percent: float
    volume: float
    relative_volume: float
    candle_range_percent: float
    real_order_sent: bool
    mode: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AIOpportunityReviewer:
    """
    Safe research-only reviewer.

    Version 1 intentionally uses deterministic local scoring instead of
    an external model. This guarantees:
    - no API cost per candle;
    - no network dependency;
    - reproducible results;
    - no ability to open or modify an order.

    The reviewer only:
    1. scores a closed candle;
    2. explains the score;
    3. appends one JSON line to its own journal;
    4. builds a daily Telegram summary.
    """

    VERSION = "AI_OPPORTUNITY_REVIEW_V1"
    MODE = "RESEARCH_SHADOW_ONLY"

    def __init__(
        self,
        journal_file: Path | str = DEFAULT_JOURNAL_FILE,
        interesting_score: float = 70.0,
    ) -> None:
        self.journal_file = Path(journal_file)
        self.interesting_score = _clamp(
            float(interesting_score),
            0.0,
            100.0,
        )

    def analyze(
        self,
        *,
        pipeline_data: dict[str, Any] | None,
        snapshot: dict[str, Any],
        symbol: str = "BTCUSDT",
        recorded_at_utc: str | None = None,
    ) -> dict[str, Any]:
        pipeline = _safe_dict(pipeline_data)
        market = _safe_dict(pipeline.get("market"))
        signal = _safe_dict(pipeline.get("signal"))
        decision = _safe_dict(pipeline.get("decision"))
        paper_order = _safe_dict(pipeline.get("paper_order"))
        leverage_risk = _safe_dict(pipeline.get("leverage_risk"))
        unified = _safe_dict(
            pipeline.get("unified_market_context")
        )

        sources = [
            unified,
            signal,
            decision,
            market,
            paper_order,
            leverage_risk,
            pipeline,
        ]

        strategy_decision = str(
            _first_present(
                sources,
                (
                    "decision",
                    "signal",
                    "side",
                    "action",
                ),
                "UNKNOWN",
            )
        ).upper()

        signal_quality = _normalize_ratio(
            _first_present(
                sources,
                (
                    "quality",
                    "confidence",
                    "signal_quality",
                    "score",
                ),
                0.0,
            )
        )

        volatility = _normalize_ratio(
            _first_present(
                [unified, market, pipeline],
                (
                    "volatility_score",
                    "volatility",
                ),
                0.0,
            )
        )

        volatility_label = str(
            unified.get("volatility", "UNKNOWN")
        ).upper()

        liquidity = _normalize_ratio(
            _first_present(
                [unified, market, pipeline],
                (
                    "liquidity_score",
                    "liquidity",
                ),
                0.0,
            )
        )

        market_regime = str(
            _first_present(
                [unified, pipeline, market],
                (
                    "market_regime",
                    "primary_regime",
                    "regime",
                ),
                "UNKNOWN",
            )
        ).upper()

        trend = str(
            unified.get("trend", "UNKNOWN")
        ).upper()
        trend_strength = _normalize_ratio(
            unified.get("trend_strength", 0.0)
        )
        atr = _safe_float(unified.get("atr"), 0.0)
        atr_percent = _safe_float(
            unified.get("atr_percent"), 0.0
        )
        volume = _safe_float(
            unified.get("volume"), 0.0
        )
        relative_volume = _safe_float(
            unified.get("relative_volume"), 0.0
        )

        price = _safe_float(snapshot.get("price"), 0.0)
        candle_high = _safe_float(
            snapshot.get("candle_high"),
            price,
        )
        candle_low = _safe_float(
            snapshot.get("candle_low"),
            price,
        )

        candle_range = max(candle_high - candle_low, 0.0)
        candle_range_percent = (
            (candle_range / price) * 100.0
            if price > 0.0
            else 0.0
        )

        score, reasons = self._calculate_score(
            strategy_decision=strategy_decision,
            signal_quality=signal_quality,
            volatility=volatility,
            liquidity=liquidity,
            market_regime=market_regime,
            candle_range_percent=candle_range_percent,
        )

        score += trend_strength * 8.0
        if relative_volume >= 1.20:
            score += 5.0
            reasons.append(
                "Volume is above its recent average."
            )
        elif relative_volume > 0.0 and relative_volume < 0.70:
            score -= 4.0
            reasons.append(
                "Volume is below its recent average."
            )
        score = _clamp(score, 0.0, 100.0)

        opportunity_decision = (
            "INTERESTING"
            if score >= self.interesting_score
            else "PASS"
        )

        confidence = self._calculate_confidence(
            signal_quality=signal_quality,
            volatility=volatility,
            liquidity=liquidity,
            market_regime=market_regime,
        )

        review = OpportunityReview(
            recorded_at_utc=(
                recorded_at_utc or utc_now_iso()
            ),
            symbol=str(symbol),
            price=round(price, 8),
            candle_high=round(candle_high, 8),
            candle_low=round(candle_low, 8),
            strategy_decision=strategy_decision,
            opportunity_decision=opportunity_decision,
            score=round(score, 2),
            confidence=round(confidence, 2),
            reasons=reasons,
            market_regime=market_regime,
            trend=trend,
            trend_strength=round(trend_strength, 4),
            signal_quality=round(signal_quality, 4),
            volatility=round(volatility, 4),
            volatility_label=volatility_label,
            liquidity=round(liquidity, 4),
            atr=round(atr, 8),
            atr_percent=round(atr_percent, 6),
            volume=round(volume, 8),
            relative_volume=round(relative_volume, 6),
            candle_range_percent=round(
                candle_range_percent,
                6,
            ),
            real_order_sent=False,
            mode=self.MODE,
            version=self.VERSION,
        )

        result = review.to_dict()
        self.append_review(result)
        return result

    def _calculate_score(
        self,
        *,
        strategy_decision: str,
        signal_quality: float,
        volatility: float,
        liquidity: float,
        market_regime: str,
        candle_range_percent: float,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []

        # Weighted, reproducible local baseline.
        score = (
            signal_quality * 45.0
            + liquidity * 25.0
            + volatility * 15.0
        )

        if strategy_decision in {
            "BUY",
            "LONG",
            "SELL",
            "SHORT",
        }:
            score += 10.0
            reasons.append(
                "Trading pipeline detected a directional candidate."
            )
        else:
            reasons.append(
                "Trading pipeline did not produce a directional candidate."
            )

        if market_regime in {
            "TREND",
            "TRENDING",
            "BULL",
            "BULLISH",
            "BEAR",
            "BEARISH",
        }:
            score += 5.0
            reasons.append(
                f"Market regime is directional: {market_regime}."
            )
        elif market_regime in {
            "RANGE",
            "RANGING",
            "SIDEWAYS",
            "CHOP",
            "CHOPPY",
        }:
            score -= 8.0
            reasons.append(
                f"Market regime may increase false signals: {market_regime}."
            )
        else:
            reasons.append(
                "Market regime is unknown or not classified."
            )

        if signal_quality >= 0.75:
            reasons.append("Signal quality is strong.")
        elif signal_quality >= 0.50:
            reasons.append("Signal quality is moderate.")
        else:
            reasons.append("Signal quality is weak or unavailable.")

        if liquidity >= 0.70:
            reasons.append("Liquidity conditions are strong.")
        elif liquidity >= 0.40:
            reasons.append("Liquidity conditions are acceptable.")
        else:
            reasons.append("Liquidity is weak or unavailable.")

        if volatility >= 0.80:
            score -= 5.0
            reasons.append(
                "Volatility is high; whipsaw risk may be elevated."
            )
        elif volatility >= 0.35:
            reasons.append(
                "Volatility is within a usable range."
            )
        else:
            score -= 4.0
            reasons.append(
                "Volatility is low or unavailable."
            )

        if candle_range_percent <= 0.0:
            score -= 5.0
            reasons.append(
                "Candle range could not be validated."
            )
        elif candle_range_percent < 0.02:
            score -= 3.0
            reasons.append(
                "Closed candle range is very small."
            )
        else:
            reasons.append(
                "Closed candle has a measurable price range."
            )

        return _clamp(score, 0.0, 100.0), reasons[:6]

    @staticmethod
    def _calculate_confidence(
        *,
        signal_quality: float,
        volatility: float,
        liquidity: float,
        market_regime: str,
    ) -> float:
        available_fields = 0

        if signal_quality > 0.0:
            available_fields += 1
        if volatility > 0.0:
            available_fields += 1
        if liquidity > 0.0:
            available_fields += 1
        if market_regime != "UNKNOWN":
            available_fields += 1

        return (available_fields / 4.0) * 100.0

    def append_review(
        self,
        review: dict[str, Any],
    ) -> None:
        self.journal_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        safe_record = dict(review)
        safe_record["real_order_sent"] = False

        with self.journal_file.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(
                    safe_record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )

    def read_reviews_for_utc_date(
        self,
        target_date_utc: str,
    ) -> list[dict[str, Any]]:
        if not self.journal_file.exists():
            return []

        reviews: list[dict[str, Any]] = []

        with self.journal_file.open(
            "r",
            encoding="utf-8",
        ) as file:
            for line in file:
                line = line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                recorded_at = str(
                    record.get("recorded_at_utc", "")
                )

                if recorded_at[:10] == target_date_utc:
                    reviews.append(record)

        return reviews

    def build_daily_summary(
        self,
        target_date_utc: str | None = None,
    ) -> str:
        if target_date_utc is None:
            target_date_utc = (
                datetime.now(timezone.utc)
                .date()
                .isoformat()
            )

        reviews = self.read_reviews_for_utc_date(
            target_date_utc
        )

        if not reviews:
            return (
                "📊 AI OPPORTUNITY REVIEW — DAILY\n\n"
                f"UTC date: {target_date_utc}\n"
                "Closed candles analyzed: 0\n"
                "Interesting opportunities: 0\n"
                "Average score: 0.00\n"
                "Highest score: 0.00\n"
                "Average ATR: 0.000000%\n"
                "Average Relative Volume: 0.000x\n"
                "Average Liquidity Score: 0.000\n"
                "Average Trend Strength: 0.000\n\n"
                "Mode: RESEARCH SHADOW ONLY\n"
                "REAL ORDERS: DISABLED"
            )

        scores = [
            _safe_float(item.get("score"), 0.0)
            for item in reviews
        ]

        atr_percent_values = [
            _safe_float(item.get("atr_percent"), 0.0)
            for item in reviews
        ]
        relative_volume_values = [
            _safe_float(item.get("relative_volume"), 0.0)
            for item in reviews
        ]
        liquidity_values = [
            _safe_float(item.get("liquidity"), 0.0)
            for item in reviews
        ]
        trend_strength_values = [
            _safe_float(item.get("trend_strength"), 0.0)
            for item in reviews
        ]

        interesting = [
            item
            for item in reviews
            if item.get("opportunity_decision")
            == "INTERESTING"
        ]

        strategy_counts = Counter(
            str(
                item.get(
                    "strategy_decision",
                    "UNKNOWN",
                )
            )
            for item in reviews
        )

        regime_counts = Counter(
            str(
                item.get(
                    "market_regime",
                    "UNKNOWN",
                )
            )
            for item in reviews
        )

        reason_counts: Counter[str] = Counter()

        for item in reviews:
            reasons = item.get("reasons", [])

            if isinstance(reasons, list):
                reason_counts.update(
                    str(reason)
                    for reason in reasons
                )

        top_reasons = reason_counts.most_common(3)
        reason_text = "\n".join(
            f"{index}. {reason} — {count}"
            for index, (reason, count)
            in enumerate(top_reasons, start=1)
        )

        if not reason_text:
            reason_text = "No reasons recorded."

        strategy_text = ", ".join(
            f"{name}: {count}"
            for name, count
            in strategy_counts.most_common()
        )

        regime_text = ", ".join(
            f"{name}: {count}"
            for name, count
            in regime_counts.most_common()
        )

        average_score = sum(scores) / len(scores)
        maximum_score = max(scores)
        average_atr_percent = (
            sum(atr_percent_values)
            / len(atr_percent_values)
        )
        average_relative_volume = (
            sum(relative_volume_values)
            / len(relative_volume_values)
        )
        average_liquidity = (
            sum(liquidity_values)
            / len(liquidity_values)
        )
        average_trend_strength = (
            sum(trend_strength_values)
            / len(trend_strength_values)
        )

        return (
            "📊 AI OPPORTUNITY REVIEW — DAILY\n\n"
            f"UTC date: {target_date_utc}\n"
            f"Closed candles analyzed: {len(reviews)}\n"
            f"Interesting opportunities: {len(interesting)}\n"
            f"Average score: {average_score:.2f}\n"
            f"Highest score: {maximum_score:.2f}\n"
            f"Market regimes: {regime_text}\n"
            f"Average ATR: {average_atr_percent:.6f}%\n"
            f"Average Relative Volume: "
            f"{average_relative_volume:.3f}x\n"
            f"Average Liquidity Score: "
            f"{average_liquidity:.3f}\n"
            f"Average Trend Strength: "
            f"{average_trend_strength:.3f}\n"
            f"Strategy decisions: {strategy_text}\n\n"
            "Most frequent observations:\n"
            f"{reason_text}\n\n"
            "Mode: RESEARCH SHADOW ONLY\n"
            "REAL ORDERS: DISABLED"
        )

    def send_daily_summary(
        self,
        send_message: Callable[[str], Any],
        target_date_utc: str | None = None,
    ) -> bool:
        """
        send_message must be a callable accepting one text argument.

        Example:
            reviewer.send_daily_summary(
                TELEGRAM_NOTIFIER.send_message
            )
        """
        summary = self.build_daily_summary(
            target_date_utc=target_date_utc
        )

        try:
            result = send_message(summary)
        except Exception:
            return False

        if isinstance(result, bool):
            return result

        return True


_DEFAULT_REVIEWER = AIOpportunityReviewer()


def review_closed_candle(
    *,
    pipeline_data: dict[str, Any] | None,
    snapshot: dict[str, Any],
    symbol: str = "BTCUSDT",
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    """
    Safe integration function for paper_live_loop.py.
    """
    return _DEFAULT_REVIEWER.analyze(
        pipeline_data=pipeline_data,
        snapshot=snapshot,
        symbol=symbol,
        recorded_at_utc=recorded_at_utc,
    )


def build_daily_opportunity_summary(
    target_date_utc: str | None = None,
) -> str:
    return _DEFAULT_REVIEWER.build_daily_summary(
        target_date_utc=target_date_utc
    )