"""
LONDON_SESSION_BREAKOUT_RETEST v1.0.0

Реализация по Strategy Implementation Contract.

Сессия London 07:00-16:00 UTC взята из config/trading_sessions.py. Отдельный
перевод часовых поясов НЕ выполняется и не нужен: проектное расписание уже
задано в UTC, а свечи приходят с UTC-метками. Введение своей таймзоны здесь
означало бы сдвиг относительно всего остального проекта.

Машина состояний (только закрытые свечи):
  RANGE_BUILDING -> 07:00-07:30 формируется opening range
  WAITING_BREAKOUT -> ждём ЗАКРЫТИЯ выше range_high
  WAITING_RETEST -> ждём возврата в зону range_high +- 0.25*ATR
  ENTRY -> свеча коснулась зоны и ЗАКРЫЛАСЬ выше range_high

Chase-entry невозможен структурно: вход достижим только из состояния
WAITING_RETEST. Возврат ЗАКРЫТИЕМ внутрь диапазона сбрасывает setup —
это инвалидация из контракта, а не эвристика.
"""

from __future__ import annotations

from datetime import datetime, timezone

from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    CandleWindow,
    StrategyDecision,
    atr,
)


# config/trading_sessions.py: TRADING_SESSIONS["LONDON"]
LONDON_START_MINUTES = 7 * 60      # 07:00 UTC
LONDON_END_MINUTES = 16 * 60       # 16:00 UTC


def _utc_minutes(open_time_ms: int) -> int:
    moment = datetime.fromtimestamp(open_time_ms / 1000.0, tz=timezone.utc)

    return moment.hour * 60 + moment.minute


def _utc_date(open_time_ms: int):
    return datetime.fromtimestamp(
        open_time_ms / 1000.0, tz=timezone.utc
    ).date()


class LondonSessionBreakoutRetest(BaseStrategy):

    strategy_key = "LONDON_SESSION_BREAKOUT_RETEST"
    version = "1.0.0"

    def _session_candles(self, window: CandleWindow) -> list:
        """Свечи текущего дня внутри London-сессии, до текущей включительно."""
        current_day = _utc_date(window.current.open_time_ms)

        collected = []

        for offset in range(len(window)):
            candle = window[-1 - offset]

            if _utc_date(candle.open_time_ms) != current_day:
                break

            minutes = _utc_minutes(candle.open_time_ms)

            if LONDON_START_MINUTES <= minutes < LONDON_END_MINUTES:
                collected.append(candle)

        collected.reverse()

        return collected

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        config = self.config
        current = window.current

        minutes = _utc_minutes(current.open_time_ms)

        if not (LONDON_START_MINUTES <= minutes < LONDON_END_MINUTES):
            return self.no_trade(
                "OUTSIDE_LONDON_SESSION",
                utc_minutes=minutes,
                session_start=LONDON_START_MINUTES,
                session_end=LONDON_END_MINUTES,
            )

        atr_value = atr(window.slice(config.warmup_bars), config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE")

        session = self._session_candles(window)

        range_end_minutes = LONDON_START_MINUTES + config.opening_range_minutes

        opening_range = [
            candle for candle in session
            if _utc_minutes(candle.open_time_ms) < range_end_minutes
        ]

        after_range = [
            candle for candle in session
            if _utc_minutes(candle.open_time_ms) >= range_end_minutes
        ]

        if not opening_range:
            return self.no_trade("OPENING_RANGE_MISSING")

        # Пока идёт формирование диапазона — входа быть не может.
        if minutes < range_end_minutes:
            return self.no_trade(
                "OPENING_RANGE_BUILDING",
                bars_collected=len(opening_range),
            )

        range_high = max(candle.high for candle in opening_range)
        range_low = min(candle.low for candle in opening_range)

        if range_high <= range_low:
            return self.no_trade("OPENING_RANGE_DEGENERATE")

        tolerance = config.retest_tolerance_atr * atr_value

        # --- машина состояний по свечам ПОСЛЕ диапазона ---
        breakout_seen = False
        retest_seen = False
        trades_taken = 0

        for candle in after_range:
            is_last = candle is current

            if not breakout_seen:
                # Пробой засчитывается только ЗАКРЫТИЕМ: фитиль означал бы
                # решение по незакрытым данным.
                if candle.close > range_high:
                    breakout_seen = True
                continue

            # Инвалидация: закрытие обратно внутрь диапазона.
            if candle.close < range_high:
                if candle.close <= range_high and candle.close >= range_low:
                    breakout_seen = False
                    retest_seen = False
                continue

            # Retest: касание зоны у границы диапазона.
            touched = candle.low <= range_high + tolerance

            if touched and candle.close > range_high:
                if is_last:
                    if trades_taken >= config.max_trades_per_session:
                        return self.no_trade(
                            "SESSION_TRADE_LIMIT_REACHED",
                            limit=config.max_trades_per_session,
                        )

                    return self.build_plan(
                        entry=candle.close,
                        atr_value=atr_value,
                        reason_code="LONDON_BREAKOUT_RETEST_CONFIRMED",
                        range_high=round(range_high, 2),
                        range_low=round(range_low, 2),
                        tolerance=round(tolerance, 2),
                        opening_range_bars=len(opening_range),
                    )

                # Сделка уже была ранее в этой сессии.
                trades_taken += 1
                retest_seen = True

        if not breakout_seen:
            return self.no_trade(
                "NO_BREAKOUT",
                range_high=round(range_high, 2),
                close=current.close,
            )

        if trades_taken >= config.max_trades_per_session:
            return self.no_trade(
                "SESSION_TRADE_LIMIT_REACHED",
                limit=config.max_trades_per_session,
            )

        return self.no_trade(
            "AWAITING_RETEST",
            range_high=round(range_high, 2),
            tolerance=round(tolerance, 2),
            retest_seen=retest_seen,
        )
