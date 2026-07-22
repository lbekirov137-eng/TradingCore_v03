from __future__ import annotations

import math
from typing import Any


class LeverageRiskEngine:
    """
    Безопасно выбирает плечо 1x / 2x / 3x
    и рассчитывает размер позиции.

    Главное правило:
    плечо не увеличивает максимально допустимый убыток.

    При капитале $1000 и риске 0.1%:
    максимальный запланированный убыток = $1.
    """

    MAX_RISK_PERCENT = 0.001
    MAX_LEVERAGE = 3

    @staticmethod
    def _score(value: Any) -> float:
        """
        Разрешает передавать оценку как 0.85 или 85.
        Возвращает значение от 0.0 до 1.0.
        """

        score = float(value)

        if score > 1.0:
            score = score / 100.0

        return max(0.0, min(score, 1.0))

    @staticmethod
    def _no_trade(reason: str) -> dict[str, Any]:
        """
        Безопасный ответ, когда открывать позицию нельзя.
        """

        return {
            "decision": "NO_TRADE",
            "allowed": False,
            "reason": reason,
            "leverage": 0,
            "maximum_allowed_leverage": 0,
            "position_size": 0.0,
            "position_notional": 0.0,
            "margin_required": 0.0,
            "risk_amount": 0.0,
            "actual_risk_amount": 0.0,
            "real_order_sent": False,
        }

    @classmethod
    def evaluate(
        cls,
        *,
        capital: float,
        entry_price: float,
        stop_price: float,
        side: str,
        signal_quality: float,
        volatility: float,
        liquidity: float,
        market_regime: str = "UNKNOWN",
        risk_percent: float = MAX_RISK_PERCENT,
        data_ok: bool = True,
        system_ok: bool = True,
    ) -> dict[str, Any]:
        """
        Проверяет условия сделки, выбирает разрешённое плечо
        и рассчитывает безопасный размер позиции.
        """

        try:
            capital = float(capital)
            entry_price = float(entry_price)
            stop_price = float(stop_price)
            risk_percent = float(risk_percent)

            signal_quality = cls._score(signal_quality)
            volatility = cls._score(volatility)
            liquidity = cls._score(liquidity)

        except (TypeError, ValueError):
            return cls._no_trade("INVALID_INPUT")

        side = str(side).strip().upper()
        market_regime = str(market_regime).strip().upper()

        if not data_ok:
            return cls._no_trade("MARKET_DATA_UNRELIABLE")

        if not system_ok:
            return cls._no_trade("SYSTEM_HEALTH_UNRELIABLE")

        if capital <= 0:
            return cls._no_trade("INVALID_CAPITAL")

        if entry_price <= 0 or stop_price <= 0:
            return cls._no_trade("INVALID_ENTRY_OR_STOP")

        if side not in {"LONG", "SHORT"}:
            return cls._no_trade("INVALID_SIDE")

        if risk_percent <= 0:
            return cls._no_trade("INVALID_RISK_PERCENT")

        if risk_percent > cls.MAX_RISK_PERCENT:
            return cls._no_trade("RISK_LIMIT_EXCEEDED")

        if side == "LONG" and stop_price >= entry_price:
            return cls._no_trade(
                "LONG_STOP_MUST_BE_BELOW_ENTRY"
            )

        if side == "SHORT" and stop_price <= entry_price:
            return cls._no_trade(
                "SHORT_STOP_MUST_BE_ABOVE_ENTRY"
            )

        stop_distance = abs(entry_price - stop_price)
        stop_distance_percent = stop_distance / entry_price

        if stop_distance_percent < 0.0005:
            return cls._no_trade("STOP_TOO_CLOSE")

        if stop_distance_percent > 0.05:
            return cls._no_trade("STOP_TOO_FAR")

        if signal_quality < 0.60:
            return cls._no_trade("SIGNAL_QUALITY_TOO_LOW")

        if liquidity < 0.50:
            return cls._no_trade("LIQUIDITY_TOO_LOW")

        if volatility > 0.90:
            return cls._no_trade("VOLATILITY_TOO_HIGH")

        bullish_regimes = {
            "BULLISH",
            "BULL",
            "UPTREND",
            "TREND_UP",
        }

        bearish_regimes = {
            "BEARISH",
            "BEAR",
            "DOWNTREND",
            "TREND_DOWN",
        }

        regime_aligned = (
            side == "LONG"
            and market_regime in bullish_regimes
        ) or (
            side == "SHORT"
            and market_regime in bearish_regimes
        )

        regime_conflict = (
            side == "LONG"
            and market_regime in bearish_regimes
        ) or (
            side == "SHORT"
            and market_regime in bullish_regimes
        )

        if regime_conflict:
            return cls._no_trade(
                "MARKET_REGIME_CONFLICT"
            )

        maximum_allowed_leverage = 1

        two_x_allowed = (
            regime_aligned
            and signal_quality >= 0.78
            and liquidity >= 0.65
            and volatility <= 0.65
            and stop_distance_percent <= 0.025
        )

        three_x_allowed = (
            regime_aligned
            and signal_quality >= 0.90
            and liquidity >= 0.80
            and volatility <= 0.40
            and stop_distance_percent <= 0.015
        )

        if two_x_allowed:
            maximum_allowed_leverage = 2

        if three_x_allowed:
            maximum_allowed_leverage = 3

        risk_amount = capital * risk_percent

        requested_position_size = (
            risk_amount / stop_distance
        )

        requested_notional = (
            requested_position_size * entry_price
        )

        required_leverage = max(
            1,
            math.ceil(requested_notional / capital),
        )

        selected_leverage = min(
            maximum_allowed_leverage,
            cls.MAX_LEVERAGE,
        )

        maximum_notional = (
            capital * selected_leverage
        )

        position_notional = min(
            requested_notional,
            maximum_notional,
        )

        position_size = (
            position_notional / entry_price
        )

        actual_risk_amount = (
            position_size * stop_distance
        )

        margin_required = (
            position_notional / selected_leverage
        )

        position_was_reduced = (
            position_notional < requested_notional
        )

        if position_was_reduced:
            reason = "POSITION_REDUCED_TO_SAFE_LIMIT"
        else:
            reason = "FULL_RISK_POSITION_ALLOWED"

        return {
            "decision": "TRADE",
            "allowed": True,
            "reason": reason,
            "side": side,
            "market_regime": market_regime,
            "regime_aligned": regime_aligned,
            "signal_quality": round(
                signal_quality,
                4,
            ),
            "volatility": round(
                volatility,
                4,
            ),
            "liquidity": round(
                liquidity,
                4,
            ),
            "leverage": selected_leverage,
            "maximum_allowed_leverage": (
                maximum_allowed_leverage
            ),
            "required_leverage": required_leverage,
            "position_size": round(
                position_size,
                8,
            ),
            "requested_position_size": round(
                requested_position_size,
                8,
            ),
            "position_notional": round(
                position_notional,
                8,
            ),
            "requested_position_notional": round(
                requested_notional,
                8,
            ),
            "margin_required": round(
                margin_required,
                8,
            ),
            "risk_percent": risk_percent,
            "risk_amount": round(
                risk_amount,
                8,
            ),
            "actual_risk_amount": round(
                actual_risk_amount,
                8,
            ),
            "stop_distance": round(
                stop_distance,
                8,
            ),
            "stop_distance_percent": round(
                stop_distance_percent,
                8,
            ),
            "position_was_reduced": (
                position_was_reduced
            ),
            "real_order_sent": False,
        }

    @classmethod
    def calculate(
        cls,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Дополнительное совместимое имя метода.
        """

        return cls.evaluate(**kwargs)