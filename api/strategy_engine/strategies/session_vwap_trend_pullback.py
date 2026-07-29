"""
SESSION_VWAP_TREND_PULLBACK v1.0.0

Реализация по Strategy Implementation Contract
(docs/STRATEGY_IMPLEMENTATION_CONTRACTS.md). Торговая идея не изобретается:
условия взяты из registry.entry_criteria, уровни — из api/trade_plan.py,
границы «боковика» — из config/adaptive_orb.py.

Последовательность (все проверки — только по закрытым свечам):
  1. режим не боковой: atr_percent в [0.8, 1.5];
  2. направление тренда: EMA20 > EMA50;
  3. цена выше session VWAP;
  4. был подтверждённый pullback в зону VWAP ± 0.5*ATR;
  5. подтверждающая свеча закрылась выше своего открытия И выше VWAP.

Зафиксированное ослабление: «направление старшего таймфрейма» в проекте
не определено (старший ТФ не загружается), поэтому используется прокси
EMA20>EMA50 на рабочем ТФ. Это записано в контракте, а не спрятано здесь.
"""

from __future__ import annotations

from datetime import datetime, timezone

from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    CandleWindow,
    StrategyDecision,
    atr,
    ema,
    session_vwap,
    swing_points,
)


# Сессия VWAP сбрасывается в 00:00 UTC (CRYPTO в config/trading_sessions.py).
SESSION_RESET_HOUR_UTC = 0


class SessionVwapTrendPullback(BaseStrategy):

    strategy_key = "SESSION_VWAP_TREND_PULLBACK"
    version = "1.0.0"

    def _session_slice(self, window: CandleWindow) -> list:
        """
        Свечи текущей UTC-сессии.

        VWAP считается от начала суток UTC, а не по скользящему окну:
        session VWAP по определению привязан к сессии, и скользящий вариант
        был бы другим индикатором с другими свойствами.
        """
        current_day = datetime.fromtimestamp(
            window.current.open_time_ms / 1000.0, tz=timezone.utc
        ).date()

        collected = []

        # Идём назад от текущей свечи: вперёд смотреть нельзя, да и не нужно.
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

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        config = self.config
        current = window.current

        history = window.slice(config.warmup_bars)

        atr_value = atr(history, config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE")

        atr_percent = atr_value / current.close * 100.0

        # 1) Боковик и переволатильность отсекаются ДО всего остального:
        # вход в боковике запрещён контрактом.
        if atr_percent < config.atr_percent_min:
            return self.no_trade(
                "RANGE_REGIME_ATR_TOO_LOW",
                atr_percent=round(atr_percent, 4),
                minimum=config.atr_percent_min,
            )

        if atr_percent > config.atr_percent_max:
            return self.no_trade(
                "VOLATILITY_TOO_HIGH",
                atr_percent=round(atr_percent, 4),
                maximum=config.atr_percent_max,
            )

        # 2) Направление тренда (прокси старшего ТФ).
        closes = window.closes(config.warmup_bars)

        fast = ema(closes, config.fast_ema)
        slow = ema(closes, config.slow_ema)

        if fast is None or slow is None:
            return self.no_trade("EMA_UNAVAILABLE")

        if fast <= slow:
            return self.no_trade(
                "TREND_NOT_UP",
                fast_ema=round(fast, 2),
                slow_ema=round(slow, 2),
            )

        # 3) Session VWAP.
        session = self._session_slice(window)
        vwap = session_vwap(session)

        if vwap is None:
            return self.no_trade(
                "VWAP_UNAVAILABLE",
                session_bars=len(session),
                note="zero session volume",
            )

        if current.close < vwap:
            return self.no_trade(
                "PRICE_BELOW_VWAP",
                close=current.close,
                vwap=round(vwap, 2),
            )

        # 4) Подтверждённый pullback: в пределах сессии до текущей свечи
        # цена должна была зайти в зону VWAP.
        zone = config.vwap_zone_atr * atr_value

        pullback_found = any(
            candle.low <= vwap + zone for candle in session[:-1]
        )

        if not pullback_found:
            return self.no_trade(
                "NO_CONFIRMED_PULLBACK",
                vwap=round(vwap, 2),
                zone=round(zone, 2),
            )

        # 5) Подтверждающая свеча: закрытие выше открытия и выше VWAP.
        # Вход возможен ТОЛЬКО после её закрытия.
        if current.close <= current.open:
            return self.no_trade(
                "CONFIRMATION_CANDLE_NOT_BULLISH",
                open=current.open,
                close=current.close,
            )

        # Market structure: последний подтверждённый swing low не должен
        # быть пробит закрытием.
        _, lows = swing_points(history)

        if lows:
            last_low = history[lows[-1]].low

            if current.close < last_low:
                return self.no_trade(
                    "STRUCTURE_BROKEN",
                    last_swing_low=round(last_low, 2),
                    close=current.close,
                )

        return self.build_plan(
            entry=current.close,
            atr_value=atr_value,
            reason_code="VWAP_TREND_PULLBACK_CONFIRMED",
            vwap=round(vwap, 2),
            atr_percent=round(atr_percent, 4),
            fast_ema=round(fast, 2),
            slow_ema=round(slow, 2),
        )
