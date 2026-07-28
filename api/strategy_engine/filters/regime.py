"""
Классификация рыночного режима и фильтры ликвидности/качества данных.

ПРИНЦИП: неопределённый режим — это НЕ разрешение торговать. Если режим
не удалось определить (мало данных, нулевая волатильность, отсутствует
объём), фильтр возвращает allowed=False. Это прямо соответствует
требованию "при неопределённом режиме стратегия не должна принудительно
выбирать сделку".

Все фильтры считаются ТОЛЬКО по видимым (закрытым) свечам.
"""

import time
from dataclasses import dataclass

from config.settings import (
    MAX_SPREAD_PERCENT,
    MIN_VOLUME_RATIO,
    MAX_ATR_PERCENT,
    MIN_ATR_PERCENT,
    MAX_DATA_AGE_SECONDS,
    MAX_CANDLE_MOVE_PERCENT,
)


class Regime:
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNDETERMINED = "UNDETERMINED"


@dataclass
class FilterResult:
    allowed: bool
    reason: str
    regime: str = Regime.UNDETERMINED
    details: dict = None

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "regime": self.regime,
            "details": self.details or {},
        }


def classify_regime(context) -> str:

    market = context.visible_market
    indicators = getattr(context, "indicators", {}) or {}

    closes = market.closes

    if len(closes) < 20:
        return Regime.UNDETERMINED

    ema = indicators.get("ema") or {}
    ema20 = ema.get("ema20")
    ema50 = ema.get("ema50")

    atr = (indicators.get("atr") or {}).get("value")
    price = closes[-1]

    if atr is None or atr != atr or atr <= 0 or price <= 0:
        return Regime.UNDETERMINED

    atr_percent = atr / price * 100

    if atr_percent > MAX_ATR_PERCENT:
        return Regime.HIGH_VOLATILITY

    if atr_percent < MIN_ATR_PERCENT:
        return Regime.LOW_VOLATILITY

    if ema20 is None or ema50 is None:
        return Regime.UNDETERMINED

    separation_percent = abs(ema20 - ema50) / price * 100

    # Слишком близкие EMA -> нет выраженного тренда.
    if separation_percent < 0.05:
        return Regime.RANGE

    return Regime.TREND_UP if ema20 > ema50 else Regime.TREND_DOWN


def resolve_now(context, now: float = None) -> float:
    """
    Определяет «текущее время» для проверки свежести данных.

    В режиме воспроизведения (бэктест/детерминированный replay) данные по
    определению не устаревшие — «сейчас» равно времени последней видимой
    свечи. Контекст сообщает об этом через атрибут now_ms. Только в
    реальном времени используется стенные часы.
    """

    if now is not None:
        return now

    now_ms = getattr(context, "now_ms", None)

    if now_ms is not None:
        return now_ms / 1000

    return time.time()


def check_data_quality(context, now: float = None) -> FilterResult:

    now = resolve_now(context, now)
    market = context.visible_market

    if len(market.timestamps) == 0:
        return FilterResult(False, "Нет рыночных данных.")

    last_ts = market.timestamps[-1]
    age_seconds = now - (last_ts / 1000)

    if age_seconds > MAX_DATA_AGE_SECONDS:
        return FilterResult(
            False,
            f"Данные устарели: последняя свеча {age_seconds:.0f} с назад "
            f"(лимит {MAX_DATA_AGE_SECONDS} с).",
        )

    # Аномальная свеча — вероятный сбой данных или флеш-крэш.
    closes = market.closes
    if len(closes) >= 2 and closes[-2] > 0:
        move_percent = abs(closes[-1] - closes[-2]) / closes[-2] * 100
        if move_percent > MAX_CANDLE_MOVE_PERCENT:
            return FilterResult(
                False,
                f"Аномальное движение свечи {move_percent:.2f}% "
                f"(лимит {MAX_CANDLE_MOVE_PERCENT}%) — вероятен сбой данных или шок.",
            )

    return FilterResult(True, "Данные свежие и без аномалий.")


def check_liquidity(context) -> FilterResult:

    market = context.visible_market
    volumes = market.volumes

    if len(volumes) < 20:
        return FilterResult(False, "Недостаточно данных об объёме.")

    recent_volume = volumes[-1]
    average_volume = sum(volumes[-20:]) / 20

    if average_volume <= 0:
        return FilterResult(False, "Нулевой средний объём — рынок неликвиден или данные повреждены.")

    ratio = recent_volume / average_volume

    if ratio < MIN_VOLUME_RATIO:
        return FilterResult(
            False,
            f"Объём {ratio:.2f}x от среднего ниже минимального {MIN_VOLUME_RATIO}x — низкая ликвидность.",
            details={"volume_ratio": round(ratio, 4)},
        )

    return FilterResult(True, "Ликвидность достаточна.", details={"volume_ratio": round(ratio, 4)})


def check_spread(spread_percent: float = None) -> FilterResult:
    """
    Проверка спреда. Если спред неизвестен (None) — это НЕ повод
    разрешить торговлю по умолчанию в demo/live; для paper-режима,
    где стакан не моделируется, отсутствие данных допускается явно.
    """

    if spread_percent is None:
        return FilterResult(True, "Спред не моделируется в paper-режиме (явное допущение).")

    if spread_percent != spread_percent or spread_percent < 0:
        return FilterResult(False, "Некорректное значение спреда.")

    if spread_percent > MAX_SPREAD_PERCENT:
        return FilterResult(
            False,
            f"Спред {spread_percent:.4f}% превышает лимит {MAX_SPREAD_PERCENT}%.",
        )

    return FilterResult(True, "Спред в допустимых пределах.")


def evaluate_all(context, spread_percent: float = None, now: float = None) -> FilterResult:
    """
    Единая точка: качество данных -> ликвидность -> спред -> режим.
    Возвращает первый неуспешный фильтр. Неопределённый режим блокирует торговлю.
    """

    data_check = check_data_quality(context, now=now)
    if not data_check.allowed:
        return data_check

    liquidity_check = check_liquidity(context)
    if not liquidity_check.allowed:
        return liquidity_check

    spread_check = check_spread(spread_percent)
    if not spread_check.allowed:
        return spread_check

    regime = classify_regime(context)

    if regime == Regime.UNDETERMINED:
        return FilterResult(False, "Рыночный режим не определён — сделка запрещена.", regime=regime)

    if regime == Regime.HIGH_VOLATILITY:
        return FilterResult(False, "Экстремальная волатильность — сделка запрещена.", regime=regime)

    if regime == Regime.LOW_VOLATILITY:
        return FilterResult(False, "Волатильность слишком низкая для достижения цели.", regime=regime)

    return FilterResult(
        True, f"Режим {regime}, фильтры пройдены.", regime=regime,
        details={"volume_ratio": (liquidity_check.details or {}).get("volume_ratio")},
    )
