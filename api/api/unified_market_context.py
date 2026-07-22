from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    if not math.isfinite(number):
        return default

    return number


def _safe_text(
    value: Any,
    default: str = "UNKNOWN",
) -> str:
    if value is None:
        return default

    text = str(value).strip()

    if not text:
        return default

    return text.upper()


def _normalize_score(
    value: Any,
    default: float = 0.0,
) -> float:
    number = _safe_float(value, default)

    if number is None:
        return default

    if number > 1.0:
        number = number / 100.0

    return max(0.0, min(1.0, number))


def _latest_number(
    values: Any,
) -> float | None:
    if not isinstance(values, list):
        return None

    for value in reversed(values):
        number = _safe_float(value)

        if number is not None:
            return number

    return None


def _average(
    values: list[float],
) -> float | None:
    if not values:
        return None

    return sum(values) / len(values)


def _relative_volume_score(
    volumes: Any,
    current_volume: float | None,
    lookback: int = 20,
) -> tuple[float, float | None]:
    """
    Returns:
    - normalized liquidity-style score 0.0..1.0
    - relative volume ratio

    Example:
    current volume / average of previous candles.
    """
    if not isinstance(volumes, list):
        return 0.0, None

    clean_values: list[float] = []

    for value in volumes:
        number = _safe_float(value)

        if number is not None and number >= 0.0:
            clean_values.append(number)

    if current_volume is None:
        current_volume = (
            clean_values[-1]
            if clean_values
            else None
        )

    if current_volume is None:
        return 0.0, None

    history = clean_values[:-1]

    if not history:
        history = clean_values

    baseline_values = history[-lookback:]
    baseline = _average(baseline_values)

    if baseline is None or baseline <= 0.0:
        return 0.0, None

    ratio = current_volume / baseline

    # Practical deterministic mapping:
    # 0.50x average -> 0.25
    # 1.00x average -> 0.50
    # 1.50x average -> 0.75
    # 2.00x average -> 1.00
    score = max(0.0, min(1.0, ratio / 2.0))

    return score, ratio


def _trend_strength_from_ema(
    ema: dict[str, Any],
    price: float | None,
) -> float:
    """
    Deterministic trend-strength proxy based on EMA separation.

    The function accepts common field names:
    fast, slow, short, long, ema_fast, ema_slow,
    ema_9, ema_20, ema_21, ema_50.
    """
    fast = None
    slow = None

    fast_keys = (
        "fast",
        "short",
        "ema_fast",
        "ema_9",
        "ema_12",
        "ema_20",
        "ema_21",
    )

    slow_keys = (
        "slow",
        "long",
        "ema_slow",
        "ema_50",
        "ema_100",
        "ema_200",
    )

    for key in fast_keys:
        fast = _safe_float(ema.get(key))

        if fast is not None:
            break

    for key in slow_keys:
        slow = _safe_float(ema.get(key))

        if slow is not None:
            break

    if (
        fast is None
        or slow is None
        or price is None
        or price <= 0.0
    ):
        return 0.0

    separation_percent = (
        abs(fast - slow) / price
    ) * 100.0

    # 0.00% -> 0.0
    # 0.50% -> 0.5
    # 1.00%+ -> 1.0
    return max(
        0.0,
        min(1.0, separation_percent),
    )


def _volatility_score_from_atr_percent(
    atr_percent: float | None,
) -> float:
    """
    Maps ATR percent to 0.0..1.0.

    This does not classify quality. It only expresses magnitude.
    """
    if atr_percent is None:
        return 0.0

    return max(
        0.0,
        min(1.0, atr_percent / 1.20),
    )


@dataclass(frozen=True)
class UnifiedMarketContext:
    recorded_at_utc: str
    symbol: str
    exchange: str
    timeframe: str

    price: float | None
    volume: float | None
    base_volume: float | None
    quote_volume: float | None
    relative_volume: float | None

    atr: float | None
    atr_percent: float | None
    volatility: str
    volatility_score: float

    liquidity: float
    trend: str
    trend_strength: float
    market_regime: str
    confidence: float
    structure: str

    ema: dict[str, Any]
    rsi: dict[str, Any]

    data_quality: str
    strategy_allowed: bool
    reasons: list[str]
    warnings: list[str]

    real_order_sent: bool
    mode: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UnifiedMarketContextBuilder:
    """
    Builds one read-only market context for:
    - Strategy
    - Risk
    - Leverage
    - AI Filter
    - AI Opportunity Review
    - Journal

    It never opens, closes, approves or rejects a trade.
    """

    VERSION = "UNIFIED_MARKET_CONTEXT_V1"
    MODE = "READ_ONLY_RESEARCH_AND_PIPELINE_CONTEXT"

    @classmethod
    def build(
        cls,
        context: Any,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot_data = _safe_dict(snapshot)

        market = _safe_dict(
            getattr(context, "market", {})
        )
        indicators = _safe_dict(
            getattr(context, "indicators", {})
        )
        regime = _safe_dict(
            getattr(context, "regime", {})
        )

        ema = _safe_dict(indicators.get("ema"))
        rsi = _safe_dict(indicators.get("rsi"))
        atr_data = _safe_dict(
            indicators.get("atr")
        )
        structure_data = _safe_dict(
            indicators.get("structure")
        )

        price = _safe_float(
            snapshot_data.get(
                "price",
                market.get("price"),
            )
        )

        if price is None:
            price = _latest_number(
                market.get("closes")
            )

        volume = _safe_float(
            market.get("volume")
        )

        if volume is None:
            volume = _latest_number(
                market.get("volumes")
            )

        base_volume = _safe_float(
            market.get("base_volume")
        )
        quote_volume = _safe_float(
            market.get("quote_volume")
        )

        liquidity, relative_volume = (
            _relative_volume_score(
                market.get("volumes"),
                volume,
            )
        )

        explicit_liquidity = None

        for source in (
            regime,
            market,
            _safe_dict(
                getattr(context, "risk", {})
            ),
        ):
            for key in (
                "liquidity_score",
                "liquidity",
            ):
                explicit_liquidity = (
                    _safe_float(source.get(key))
                )

                if explicit_liquidity is not None:
                    break

            if explicit_liquidity is not None:
                break

        if explicit_liquidity is not None:
            liquidity = _normalize_score(
                explicit_liquidity
            )

        atr = _safe_float(
            regime.get(
                "atr",
                atr_data.get("value"),
            )
        )

        atr_percent = _safe_float(
            regime.get("atr_percent")
        )

        if (
            atr_percent is None
            and atr is not None
            and price is not None
            and price > 0.0
        ):
            atr_percent = (
                atr / price
            ) * 100.0

        volatility = _safe_text(
            regime.get("volatility")
        )

        if volatility == "UNKNOWN":
            if atr_percent is None:
                volatility = "UNKNOWN"
            elif atr_percent >= 1.20:
                volatility = "HIGH"
            elif atr_percent <= 0.25:
                volatility = "LOW"
            else:
                volatility = "NORMAL"

        volatility_score = (
            _volatility_score_from_atr_percent(
                atr_percent
            )
        )

        trend = _safe_text(
            regime.get(
                "trend",
                ema.get("trend"),
            )
        )

        market_regime = _safe_text(
            regime.get(
                "primary_regime",
                regime.get("market_regime"),
            )
        )

        structure = _safe_text(
            regime.get(
                "structure",
                structure_data.get(
                    "structure",
                    structure_data.get("trend"),
                ),
            )
        )

        trend_strength = _normalize_score(
            regime.get(
                "trend_strength",
                _trend_strength_from_ema(
                    ema=ema,
                    price=price,
                ),
            )
        )

        confidence = _normalize_score(
            regime.get("confidence")
        )

        data_quality = _safe_text(
            regime.get(
                "data_quality",
                "UNKNOWN",
            )
        )

        strategy_allowed = bool(
            regime.get(
                "strategy_allowed",
                False,
            )
        )

        reasons = regime.get("reasons", [])
        warnings = regime.get("warnings", [])

        if not isinstance(reasons, list):
            reasons = [str(reasons)]

        if not isinstance(warnings, list):
            warnings = [str(warnings)]

        result = UnifiedMarketContext(
            recorded_at_utc=utc_now_iso(),
            symbol=str(
                getattr(context, "symbol", "")
            ),
            exchange=str(
                getattr(context, "exchange", "")
            ),
            timeframe=str(
                getattr(context, "timeframe", "")
            ),
            price=(
                round(price, 8)
                if price is not None
                else None
            ),
            volume=(
                round(volume, 8)
                if volume is not None
                else None
            ),
            base_volume=(
                round(base_volume, 8)
                if base_volume is not None
                else None
            ),
            quote_volume=(
                round(quote_volume, 8)
                if quote_volume is not None
                else None
            ),
            relative_volume=(
                round(relative_volume, 6)
                if relative_volume is not None
                else None
            ),
            atr=(
                round(atr, 8)
                if atr is not None
                else None
            ),
            atr_percent=(
                round(atr_percent, 6)
                if atr_percent is not None
                else None
            ),
            volatility=volatility,
            volatility_score=round(
                volatility_score,
                4,
            ),
            liquidity=round(liquidity, 4),
            trend=trend,
            trend_strength=round(
                trend_strength,
                4,
            ),
            market_regime=market_regime,
            confidence=round(confidence, 4),
            structure=structure,
            ema=ema,
            rsi=rsi,
            data_quality=data_quality,
            strategy_allowed=strategy_allowed,
            reasons=[
                str(reason)
                for reason in reasons
            ],
            warnings=[
                str(warning)
                for warning in warnings
            ],
            real_order_sent=False,
            mode=cls.MODE,
            version=cls.VERSION,
        )

        return result.to_dict()


def build_unified_market_context(
    *,
    context: Any,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Safe public integration function.
    """
    return UnifiedMarketContextBuilder.build(
        context=context,
        snapshot=snapshot,
    )