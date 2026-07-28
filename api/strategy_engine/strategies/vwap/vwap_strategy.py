"""
Session VWAP Trend Pullback.

Логика (long-only, spot, без плеча):
  1. Тренд старшего таймфрейма: EMA20 > EMA50 (bullish) — иначе NO_TRADE.
     (Строгий higher-timeframe 1H->15M->5M каскад требует загрузки
     нескольких таймфреймов; текущая реализация аппроксимирует его
     через EMA-структуру на рабочем таймфрейме — это ЗАДОКУМЕНТИРОВАННОЕ
     упрощение, см. AUTOTRADING_PRODUCTION_READINESS.md.)
  2. Цена должна быть ВЫШЕ session VWAP (торгуем по тренду сессии).
  3. Pullback: цена откатила к VWAP в пределах допуска — вход не
     "в погоне" (no chasing): если цена ушла слишком далеко от VWAP,
     сигнала нет.
  4. Подтверждение: последняя свеча закрылась выше предыдущей
     (разворот отката вверх).
  5. Стоп — под минимумом отката (структурный), не произвольный ATR.
  6. Тейк — из минимального R:R.

При недостатке данных/объёма, при флэте или конфликте условий —
всегда NO_TRADE.
"""

from api.strategy_engine.strategies.vwap.vwap import calculate_session_vwap
from api.strategy_engine.filters.regime import evaluate_all

from config.settings import MIN_RISK_REWARD


class VWAPTrendPullbackStrategy:

    NAME = "VWAP_TREND_PULLBACK"

    APPLY_FILTERS = True

    # Насколько близко к VWAP должна подойти цена, чтобы считать это
    # откатом (в долях от средн. абс. отклонения по сессии).
    PULLBACK_TOLERANCE_FACTOR = 0.75

    # Максимальное удаление от VWAP, выше которого вход считается
    # "погоней" за ценой и запрещён.
    MAX_EXTENSION_FACTOR = 2.0

    MIN_CANDLES_IN_SESSION = 10

    STOP_BUFFER_ATR_FACTOR = 0.2

    @staticmethod
    def generate(context, apply_filters: bool = None):

        apply_filters = (
            VWAPTrendPullbackStrategy.APPLY_FILTERS if apply_filters is None else apply_filters
        )

        if apply_filters:
            filter_result = evaluate_all(context)
            if not filter_result.allowed:
                return VWAPTrendPullbackStrategy._no_trade(
                    filter_result.reason, metadata={"regime": filter_result.regime},
                )

        vwap_data = calculate_session_vwap(context)

        if vwap_data is None:
            return VWAPTrendPullbackStrategy._no_trade("VWAP недоступен (нет объёма или данных сессии).")

        if vwap_data["candles_in_session"] < VWAPTrendPullbackStrategy.MIN_CANDLES_IN_SESSION:
            return VWAPTrendPullbackStrategy._no_trade("Слишком мало свечей с начала сессии.")

        indicators = getattr(context, "indicators", {}) or {}

        ema = indicators.get("ema") or {}
        ema20 = ema.get("ema20")
        ema50 = ema.get("ema50")

        if ema20 is None or ema50 is None:
            return VWAPTrendPullbackStrategy._no_trade("EMA недоступны.")

        if not (ema20 > ema50):
            return VWAPTrendPullbackStrategy._no_trade("Тренд не бычий (EMA20 <= EMA50) — вход запрещён.")

        atr_data = indicators.get("atr") or {}
        atr = atr_data.get("value")

        if atr is None or atr != atr or atr <= 0:
            return VWAPTrendPullbackStrategy._no_trade("ATR недоступен или некорректен.")

        market = context.visible_market
        closes = market.closes
        lows = market.lows

        if len(closes) < 3:
            return VWAPTrendPullbackStrategy._no_trade("Недостаточно свечей.")

        last_close = closes[-1]
        previous_close = closes[-2]
        vwap = vwap_data["vwap"]
        distance = vwap_data["distance"]
        mean_abs_deviation = vwap_data["mean_abs_deviation"]

        if mean_abs_deviation <= 0:
            return VWAPTrendPullbackStrategy._no_trade("Нулевая волатильность относительно VWAP (флэт).")

        if distance <= 0:
            return VWAPTrendPullbackStrategy._no_trade("Цена ниже VWAP — long не рассматривается.")

        pullback_threshold = mean_abs_deviation * VWAPTrendPullbackStrategy.PULLBACK_TOLERANCE_FACTOR
        max_extension = mean_abs_deviation * VWAPTrendPullbackStrategy.MAX_EXTENSION_FACTOR

        if distance > max_extension:
            return VWAPTrendPullbackStrategy._no_trade(
                "Цена слишком далеко от VWAP — вход в погоне запрещён."
            )

        if distance > pullback_threshold:
            return VWAPTrendPullbackStrategy._no_trade("Отката к VWAP не произошло.")

        # Подтверждение: откат развернулся вверх.
        if last_close <= previous_close:
            return VWAPTrendPullbackStrategy._no_trade("Нет подтверждающей свечи разворота отката.")

        session_start = vwap_data["start_index"]
        recent_lows = lows[max(session_start, len(lows) - 6):]

        if not recent_lows:
            return VWAPTrendPullbackStrategy._no_trade("Нет данных для структурного стопа.")

        structural_low = min(recent_lows)

        stop = structural_low - atr * VWAPTrendPullbackStrategy.STOP_BUFFER_ATR_FACTOR

        entry = last_close
        risk_distance = entry - stop

        if risk_distance <= 0:
            return VWAPTrendPullbackStrategy._no_trade("Некорректная дистанция до стопа.")

        tp1 = entry + risk_distance * MIN_RISK_REWARD
        tp2 = entry + risk_distance * 3

        return {
            "approved": True,
            "strategy": VWAPTrendPullbackStrategy.NAME,
            "direction": "LONG",
            "trade_plan": {
                "entry": entry,
                "stop_loss": stop,
                "take_profit": {
                    "tp1": tp1,
                    "tp2": tp2,
                    "risk_reward": f"1:{MIN_RISK_REWARD:.0f} / 1:3",
                },
                "risk_reward": f"1:{MIN_RISK_REWARD:.0f} / 1:3",
            },
            "confidence": round(min(1.0, pullback_threshold / max(distance, 1e-9)), 3),
            "reason": "Бычий тренд + откат к session VWAP + подтверждение разворота.",
            "metadata": {
                "session": vwap_data["session"],
                "vwap": vwap,
                "distance_to_vwap": distance,
                "structural_low": structural_low,
            },
        }

    @staticmethod
    def _no_trade(reason, metadata=None):
        return {
            "approved": False,
            "strategy": VWAPTrendPullbackStrategy.NAME,
            "direction": None,
            "trade_plan": None,
            "confidence": 0.0,
            "reason": reason,
            "metadata": metadata or {},
        }
