"""
Кандидаты @2.0.0: исполнение 1H, контекст 4H, структурный стоп.

Отличия от @1.0.0 (версии @1.0.0 не изменяются и остаются baseline):

  1. РЕШЕНИЯ ПО ЗАКРЫТЫМ 1H СВЕЧАМ. Волатильность 1H (median ATR 0.63%
     против 0.15% на 5m) — единственное, что делает издержки приемлемыми:
     cost_in_R = cost_rate / stop_percent.

  2. КОНТЕКСТ ТОЛЬКО ИЗ ЗАКРЫТЫХ 4H СВЕЧЕЙ. 4H свеча, внутри которой
     находится текущий час, ещё не закрыта, и её close/high/low содержат
     будущее. align_context отдаёт только полностью закрытые.

  3. СТРУКТУРНЫЙ СТОП вместо фиксированного 1 ATR. Стоп ставится под
     подтверждённой точкой инвалидации; ATR задаёт ГРАНИЦЫ ДОПУСТИМОСТИ,
     а не сам уровень. Стоп никогда не двигается ради улучшения cost
     ratio — если структура даёт неподходящий стоп, сделки просто нет.

  4. COST GATE ДО ПЛАНА. Экономика проверяется раньше, чем строится
     TradePlan, поэтому нежизнеспособная сделка не появляется вовсе.

Инварианты: LONG only, одна сделка за сессию, риск 0.1%, плечо 1x,
никаких обращений к бирже, никакого глобального состояния.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from api.strategy_engine.cost_gate import CostGateConfig, evaluate_cost_viability
from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    Candle,
    CandleWindow,
    StrategyConfig,
    StrategyDecision,
    atr,
    ema,
    session_vwap,
    swing_points,
)
from api.strategy_engine.timeframes import align_context


# Риск на сделку в деньгах: 0.1% от 1000 USD (config/settings.py).
RISK_AMOUNT_USD = 1.0


@dataclass(frozen=True)
class StructuralConfig(StrategyConfig):
    """
    Конфигурация @2.0.0. Отдельный класс — значит отдельный
    parameter_hash, поэтому результаты @1.0.0 и @2.0.0 несмешиваемы.
    """

    execution_timeframe: str = "1H"
    context_timeframe: str = "4H"

    # Границы ДОПУСТИМОСТИ структурного стопа, не способ его выбрать.
    # DEFAULT_NOT_OPTIMIZED: выбраны до прогона, вне зависимости от
    # результатов. Ниже 1.0 ATR структура неотличима от шума; выше
    # 4.0 ATR позиция становится слишком мелкой, а стоп — не структурным.
    min_stop_atr: float = 1.0
    max_stop_atr: float = 4.0

    # Буфер под структурным уровнем, чтобы стоп не стоял ровно на
    # экстремуме, который часто прокалывают.
    stop_buffer_atr: float = 0.1

    # Цель: gross R:R. Проектный минимум 2.0; берём 3.0, потому что после
    # издержек (~0.25R) чистый R:R обязан остаться выше 1.5.
    target_rr: float = 3.0

    warmup_bars: int = 60
    structure_lookback: int = 30


class StructuralBase(BaseStrategy):
    """Общая часть кандидатов @2.0.0: стоп, цель, cost gate."""

    version = "2.0.0"

    def __init__(
        self,
        config: StructuralConfig | None = None,
        cost_gate_config: CostGateConfig | None = None,
    ) -> None:
        super().__init__(config or StructuralConfig())
        self.cost_gate_config = cost_gate_config or CostGateConfig()

    # ------------------------------------------------------ структура

    def structural_stop(
        self,
        window: CandleWindow,
        atr_value: float,
    ) -> tuple[float | None, str, dict[str, Any]]:
        """
        Уровень инвалидации из подтверждённой структуры.

        Берётся последний ПОДТВЕРЖДЁННЫЙ swing low (с барами справа),
        поэтому последние бары структуру не образуют — swing, объявленный
        на текущей свече, был бы утечкой будущего.
        """
        config = self.config
        history = window.slice(config.structure_lookback)

        _, lows = swing_points(history)

        if not lows:
            return None, "NO_STRUCTURAL_LEVEL", {}

        level = history[lows[-1]].low
        stop = level - config.stop_buffer_atr * atr_value

        return stop, "OK", {
            "swing_low": round(level, 2),
            "stop_buffer": round(config.stop_buffer_atr * atr_value, 2),
        }

    def finalise(
        self,
        entry: float,
        stop: float,
        atr_value: float,
        reason_code: str,
        **diagnostics: Any,
    ) -> StrategyDecision:
        """
        Проверяет стоп на допустимость и экономику, затем строит план.

        Порядок важен: сначала структурная допустимость (стоп не слишком
        узкий и не слишком широкий), затем экономика. Так reason_code
        называет первопричину, а не следствие.
        """
        config = self.config

        if stop <= 0 or stop >= entry:
            return self.no_trade("INVALID_STRUCTURAL_STOP", stop=stop, entry=entry)

        stop_distance = entry - stop
        stop_in_atr = stop_distance / atr_value

        if stop_in_atr < config.min_stop_atr:
            return self.no_trade(
                "STRUCTURAL_STOP_TOO_TIGHT",
                stop_in_atr=round(stop_in_atr, 3),
                minimum=config.min_stop_atr,
                **diagnostics,
            )

        if stop_in_atr > config.max_stop_atr:
            return self.no_trade(
                "STRUCTURAL_STOP_TOO_WIDE",
                stop_in_atr=round(stop_in_atr, 3),
                maximum=config.max_stop_atr,
                **diagnostics,
            )

        take_profit = entry + config.target_rr * stop_distance

        # Экономика — до построения плана.
        viability = evaluate_cost_viability(
            entry=entry,
            stop=stop,
            take_profit=take_profit,
            risk_amount=RISK_AMOUNT_USD,
            config=self.cost_gate_config,
        )

        if not viability["viable"]:
            return self.no_trade(
                viability["reason_code"],
                estimated_cost_r=viability["estimated_cost_r"],
                net_rr_after_costs=viability["net_rr_after_costs"],
                stop_in_atr=round(stop_in_atr, 3),
                **diagnostics,
            )

        # Размер позиции от ширины стопа: риск остаётся 0.1% при любой
        # ширине, что и есть требование «position size уменьшается
        # автоматически».
        quantity = RISK_AMOUNT_USD / stop_distance

        return StrategyDecision(
            strategy_key=self.strategy_key,
            version=self.version,
            signal="BUY",
            reason_code=reason_code,
            entry=round(entry, 2),
            stop=round(stop, 2),
            take_profit_1=round(entry + stop_distance * 2.0, 2),
            take_profit_2=round(take_profit, 2),
            risk_reward=round(config.target_rr, 4),
            diagnostics={
                **diagnostics,
                "stop_in_atr": round(stop_in_atr, 3),
                "quantity": round(quantity, 8),
                "position_notional": round(quantity * entry, 2),
                "estimated_cost_r": viability["estimated_cost_r"],
                "net_rr_after_costs": viability["net_rr_after_costs"],
                "risk_amount": RISK_AMOUNT_USD,
                "leverage": 1,
            },
        )

    # -------------------------------------------------------- контекст

    def context_is_bullish(
        self,
        window: CandleWindow,
        context_candles: Sequence[Candle],
    ) -> tuple[bool, dict[str, Any]]:
        """
        Направление старшего таймфрейма по ЗАКРЫТЫМ 4H свечам.

        В @1.0.0 старший ТФ отсутствовал и подменялся EMA на рабочем ТФ —
        зафиксированное ослабление. Здесь он настоящий.
        """
        latest = align_context(
            window.current, context_candles, self.config.context_timeframe
        )

        if latest is None:
            return False, {"context": "UNAVAILABLE"}

        closed = [
            candle for candle in context_candles
            if candle.open_time_ms <= latest.open_time_ms
        ]

        closes = [candle.close for candle in closed]

        fast = ema(closes, 20)
        slow = ema(closes, 50)

        if fast is None or slow is None:
            return False, {"context": "INSUFFICIENT_CONTEXT_HISTORY"}

        return fast > slow, {
            "context_close": round(latest.close, 2),
            "context_fast_ema": round(fast, 2),
            "context_slow_ema": round(slow, 2),
            "context_open_time_ms": latest.open_time_ms,
        }

    def evaluate_with_context(
        self,
        candles: Sequence[Candle],
        index: int,
        context_candles: Sequence[Candle],
    ) -> StrategyDecision:
        window = CandleWindow(candles, index)

        if len(window) < self.required_warmup_bars:
            return self.no_trade(
                "INSUFFICIENT_WARMUP",
                available=len(window),
                required=self.required_warmup_bars,
            )

        return self._evaluate_context(window, context_candles)

    def _evaluate_context(
        self,
        window: CandleWindow,
        context_candles: Sequence[Candle],
    ) -> StrategyDecision:
        raise NotImplementedError

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        # Без контекста кандидат @2.0.0 работать не должен: 4H-направление
        # входит в контракт, и молча обойтись без него нельзя.
        return self.no_trade("CONTEXT_REQUIRED")


class SessionVwapTrendPullbackV2(StructuralBase):

    strategy_key = "SESSION_VWAP_TREND_PULLBACK_V2"

    def _session_slice(self, window: CandleWindow) -> list[Candle]:
        current_day = datetime.fromtimestamp(
            window.current.open_time_ms / 1000.0, tz=timezone.utc
        ).date()

        collected = []

        for offset in range(len(window)):
            candle = window[-1 - offset]

            day = datetime.fromtimestamp(
                candle.open_time_ms / 1000.0, tz=timezone.utc
            ).date()

            if day != current_day:
                break

            collected.append(candle)

        collected.reverse()

        return collected

    def _evaluate_context(self, window, context_candles) -> StrategyDecision:
        config = self.config
        current = window.current

        atr_value = atr(window.slice(config.warmup_bars), config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE")

        bullish, context = self.context_is_bullish(window, context_candles)

        if not bullish:
            return self.no_trade("CONTEXT_NOT_BULLISH", **context)

        session = self._session_slice(window)
        vwap = session_vwap(session)

        if vwap is None:
            return self.no_trade("VWAP_UNAVAILABLE", **context)

        if current.close < vwap:
            return self.no_trade(
                "PRICE_BELOW_VWAP", vwap=round(vwap, 2), **context
            )

        zone = config.vwap_zone_atr * atr_value

        if not any(candle.low <= vwap + zone for candle in session[:-1]):
            return self.no_trade("NO_CONFIRMED_PULLBACK", **context)

        if current.close <= current.open:
            return self.no_trade("CONFIRMATION_CANDLE_NOT_BULLISH", **context)

        stop, status, structure = self.structural_stop(window, atr_value)

        if stop is None:
            return self.no_trade(status, **context)

        return self.finalise(
            entry=current.close,
            stop=stop,
            atr_value=atr_value,
            reason_code="VWAP_TREND_PULLBACK_V2_CONFIRMED",
            vwap=round(vwap, 2),
            **structure,
            **context,
        )


class LondonSessionBreakoutRetestV2(StructuralBase):
    """
    London на 1H: opening range = ПЕРВАЯ свеча сессии (07:00-08:00 UTC).

    В @1.0.0 диапазон строился за 30 минут. На 1H минимальная единица —
    час, поэтому диапазон переопределён. Это зафиксировано в Decision
    Record как осознанное изменение контракта, а не подгонка.
    """

    strategy_key = "LONDON_SESSION_BREAKOUT_RETEST_V2"

    LONDON_START_HOUR = 7
    LONDON_END_HOUR = 16

    def _evaluate_context(self, window, context_candles) -> StrategyDecision:
        config = self.config
        current = window.current

        moment = datetime.fromtimestamp(
            current.open_time_ms / 1000.0, tz=timezone.utc
        )

        if not (self.LONDON_START_HOUR <= moment.hour < self.LONDON_END_HOUR):
            return self.no_trade("OUTSIDE_LONDON_SESSION", hour=moment.hour)

        atr_value = atr(window.slice(config.warmup_bars), config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE")

        bullish, context = self.context_is_bullish(window, context_candles)

        if not bullish:
            return self.no_trade("CONTEXT_NOT_BULLISH", **context)

        # Свечи сессии текущего дня.
        session: list[Candle] = []

        for offset in range(len(window)):
            candle = window[-1 - offset]
            stamp = datetime.fromtimestamp(
                candle.open_time_ms / 1000.0, tz=timezone.utc
            )

            if stamp.date() != moment.date():
                break

            if self.LONDON_START_HOUR <= stamp.hour < self.LONDON_END_HOUR:
                session.append(candle)

        session.reverse()

        if len(session) < 2:
            return self.no_trade("OPENING_RANGE_BUILDING", bars=len(session))

        opening = session[0]
        after = session[1:]

        range_high = opening.high
        range_low = opening.low

        if range_high <= range_low:
            return self.no_trade("OPENING_RANGE_DEGENERATE")

        tolerance = config.retest_tolerance_atr * atr_value

        breakout = False
        trades_taken = 0

        for candle in after:
            is_last = candle is current

            if not breakout:
                if candle.close > range_high:
                    breakout = True
                continue

            if range_low <= candle.close < range_high:
                breakout = False
                continue

            touched = candle.low <= range_high + tolerance

            if touched and candle.close > range_high:
                if is_last:
                    if trades_taken >= config.max_trades_per_session:
                        return self.no_trade("SESSION_TRADE_LIMIT_REACHED")

                    stop, status, structure = self.structural_stop(
                        window, atr_value
                    )

                    # Инвалидация ретеста — нижняя граница диапазона, если
                    # структура даёт уровень выше неё.
                    if stop is None or stop > range_low:
                        stop = range_low - config.stop_buffer_atr * atr_value
                        structure = {"stop_source": "RANGE_LOW"}
                    else:
                        structure = {**structure, "stop_source": "SWING_LOW"}

                    return self.finalise(
                        entry=candle.close,
                        stop=stop,
                        atr_value=atr_value,
                        reason_code="LONDON_BREAKOUT_RETEST_V2_CONFIRMED",
                        range_high=round(range_high, 2),
                        range_low=round(range_low, 2),
                        **structure,
                        **context,
                    )

                trades_taken += 1

        if not breakout:
            return self.no_trade(
                "NO_BREAKOUT", range_high=round(range_high, 2), **context
            )

        if trades_taken >= config.max_trades_per_session:
            return self.no_trade("SESSION_TRADE_LIMIT_REACHED", **context)

        return self.no_trade("AWAITING_RETEST", **context)


class TrendPullbackEmaStructureV2(StructuralBase):

    strategy_key = "TREND_PULLBACK_EMA_STRUCTURE_V2"

    def _evaluate_context(self, window, context_candles) -> StrategyDecision:
        config = self.config
        current = window.current

        history = window.slice(config.warmup_bars)

        atr_value = atr(history, config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE")

        bullish, context = self.context_is_bullish(window, context_candles)

        if not bullish:
            return self.no_trade("CONTEXT_NOT_BULLISH", **context)

        closes = window.closes(config.warmup_bars)

        fast = ema(closes, config.fast_ema)
        slow = ema(closes, config.slow_ema)

        if fast is None or slow is None:
            return self.no_trade("EMA_UNAVAILABLE", **context)

        if fast <= slow:
            return self.no_trade("EMA_NOT_ALIGNED", **context)

        structure_window = history[-config.structure_lookback :]
        highs, lows = swing_points(structure_window)

        if len(lows) < config.min_structure_confirmations:
            return self.no_trade(
                "STRUCTURE_UNCONFIRMED", confirmed_lows=len(lows), **context
            )

        recent_lows = [structure_window[position].low for position in lows]

        if not all(
            recent_lows[i] < recent_lows[i + 1]
            for i in range(len(recent_lows) - 1)
        ):
            return self.no_trade("STRUCTURE_CONTRADICTS_TREND", **context)

        zone_low, zone_high = min(fast, slow), max(fast, slow)

        if not any(
            candle.low <= zone_high and candle.high >= zone_low
            for candle in history[-config.structure_lookback : -1]
        ):
            return self.no_trade("NO_PULLBACK_INTO_ZONE", **context)

        if not highs:
            return self.no_trade("NO_CONFIRMED_SWING_HIGH", **context)

        last_swing_high = structure_window[highs[-1]].high

        if current.close <= last_swing_high:
            return self.no_trade(
                "NO_CONTINUATION",
                last_swing_high=round(last_swing_high, 2),
                **context,
            )

        stop, status, structure = self.structural_stop(window, atr_value)

        if stop is None:
            return self.no_trade(status, **context)

        return self.finalise(
            entry=current.close,
            stop=stop,
            atr_value=atr_value,
            reason_code="TREND_PULLBACK_V2_CONFIRMED",
            **structure,
            **context,
        )


V2_IMPLEMENTATIONS = {
    SessionVwapTrendPullbackV2.strategy_key: SessionVwapTrendPullbackV2,
    LondonSessionBreakoutRetestV2.strategy_key: LondonSessionBreakoutRetestV2,
    TrendPullbackEmaStructureV2.strategy_key: TrendPullbackEmaStructureV2,
}
