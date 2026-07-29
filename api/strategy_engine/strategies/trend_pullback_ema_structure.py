"""
TREND_PULLBACK_EMA_STRUCTURE v1.0.0

Реализация по Strategy Implementation Contract.

Условия (только закрытые свечи):
  1. EMA alignment: EMA20 > EMA50;
  2. подтверждённая структура: не менее min_structure_confirmations
     последовательно повышающихся swing low;
  3. pullback в допустимую зону EMA50..EMA20;
  4. continuation: закрытие выше предыдущего подтверждённого swing high;
  5. запрет при противоречии: EMA вверх, а структура даёт lower-low.

Пункт 5 — не перестраховка. Расхождение тренда и структуры означает, что
два независимых признака противоречат друг другу; вход в такой момент
опирается на то, что один из них уже неверен.

Swing-точки берутся только ПОДТВЕРЖДЁННЫЕ (с барами справа), поэтому
последние бары структуру не образуют — иначе это была бы утечка будущего.
"""

from __future__ import annotations

from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    CandleWindow,
    StrategyDecision,
    atr,
    ema,
    swing_points,
)


class TrendPullbackEmaStructure(BaseStrategy):

    strategy_key = "TREND_PULLBACK_EMA_STRUCTURE"
    version = "1.0.0"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        config = self.config
        current = window.current

        history = window.slice(config.warmup_bars)

        atr_value = atr(history, config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE")

        closes = window.closes(config.warmup_bars)

        fast = ema(closes, config.fast_ema)
        slow = ema(closes, config.slow_ema)

        if fast is None or slow is None:
            return self.no_trade("EMA_UNAVAILABLE")

        # 1) EMA alignment.
        if fast <= slow:
            return self.no_trade(
                "EMA_NOT_ALIGNED",
                fast_ema=round(fast, 2),
                slow_ema=round(slow, 2),
            )

        # 2) Структура: подтверждённые swing low.
        structure_window = history[-config.structure_lookback :]

        highs, lows = swing_points(structure_window)

        if len(lows) < config.min_structure_confirmations:
            return self.no_trade(
                "STRUCTURE_UNCONFIRMED",
                confirmed_lows=len(lows),
                required=config.min_structure_confirmations,
            )

        recent_lows = [structure_window[position].low for position in lows]

        rising = all(
            recent_lows[position] < recent_lows[position + 1]
            for position in range(len(recent_lows) - 1)
        )

        # 5) Противоречие EMA и структуры — явный запрет.
        if not rising:
            return self.no_trade(
                "STRUCTURE_CONTRADICTS_TREND",
                note="EMA stack is bullish but swing lows are not rising",
                swing_lows=[round(value, 2) for value in recent_lows],
            )

        # 3) Pullback в зону EMA50..EMA20.
        zone_low = min(fast, slow)
        zone_high = max(fast, slow)

        pullback_found = any(
            candle.low <= zone_high and candle.high >= zone_low
            for candle in history[-config.structure_lookback : -1]
        )

        if not pullback_found:
            return self.no_trade(
                "NO_PULLBACK_INTO_ZONE",
                zone_low=round(zone_low, 2),
                zone_high=round(zone_high, 2),
            )

        # 4) Continuation: закрытие выше последнего подтверждённого swing high.
        if not highs:
            return self.no_trade(
                "NO_CONFIRMED_SWING_HIGH",
                lookback=config.structure_lookback,
            )

        last_swing_high = structure_window[highs[-1]].high

        if current.close <= last_swing_high:
            return self.no_trade(
                "NO_CONTINUATION",
                last_swing_high=round(last_swing_high, 2),
                close=current.close,
            )

        # Инвалидация: закрытие ниже подтверждённого higher-low.
        last_swing_low = recent_lows[-1]

        if current.close < last_swing_low:
            return self.no_trade(
                "STRUCTURE_BROKEN",
                last_swing_low=round(last_swing_low, 2),
                close=current.close,
            )

        return self.build_plan(
            entry=current.close,
            atr_value=atr_value,
            reason_code="TREND_PULLBACK_CONTINUATION_CONFIRMED",
            fast_ema=round(fast, 2),
            slow_ema=round(slow, 2),
            last_swing_high=round(last_swing_high, 2),
            last_swing_low=round(last_swing_low, 2),
            confirmed_lows=len(lows),
        )
