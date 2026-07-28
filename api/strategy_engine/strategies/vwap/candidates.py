"""
Кандидаты для исследования VWAP Trend Pullback — оцениваются ОТДЕЛЬНО
от baseline и друг от друга, ни один не объединяется в единую
"улучшенную" стратегию без отдельного, задокументированного решения.

VWAPTrendPullbackStrategy.generate — @staticmethod, читающий константы
класса по имени базового класса напрямую (не через cls), поэтому простое
наследование с переопределением атрибутов класса не сработало бы —
переопределённые константы тихо игнорировались бы. Вместо этого каждый
кандидат — небольшая самостоятельная реализация той же логики с одним
изменённым параметром, а не патч базового класса.
"""

from api.strategy_engine.strategies.vwap.vwap import calculate_session_vwap
from api.strategy_engine.strategies.vwap.vwap_strategy import VWAPTrendPullbackStrategy
from api.strategy_engine.filters.regime import evaluate_all

from config.settings import MIN_RISK_REWARD


def _vwap_candidate(context, name: str, pullback_tolerance_factor: float,
                     max_extension_factor: float = 2.0, require_volume_confirmation: bool = False,
                     apply_filters: bool = True):

    def no_trade(reason):
        return {
            "approved": False, "strategy": name, "direction": None,
            "trade_plan": None, "confidence": 0.0, "reason": reason, "metadata": {},
        }

    if apply_filters:
        filter_result = evaluate_all(context)
        if not filter_result.allowed:
            return no_trade(filter_result.reason)

    vwap_data = calculate_session_vwap(context)
    if vwap_data is None:
        return no_trade("VWAP недоступен.")
    if vwap_data["candles_in_session"] < VWAPTrendPullbackStrategy.MIN_CANDLES_IN_SESSION:
        return no_trade("Слишком мало свечей с начала сессии.")

    indicators = getattr(context, "indicators", {}) or {}
    ema = indicators.get("ema") or {}
    ema20, ema50 = ema.get("ema20"), ema.get("ema50")

    if ema20 is None or ema50 is None or not (ema20 > ema50):
        return no_trade("Тренд не бычий или EMA недоступны.")

    atr = (indicators.get("atr") or {}).get("value")
    if atr is None or atr != atr or atr <= 0:
        return no_trade("ATR недоступен или некорректен.")

    market = context.visible_market
    closes, lows = market.closes, market.lows

    if len(closes) < 3:
        return no_trade("Недостаточно свечей.")

    last_close, previous_close = closes[-1], closes[-2]
    distance = vwap_data["distance"]
    mean_abs_deviation = vwap_data["mean_abs_deviation"]

    if mean_abs_deviation <= 0:
        return no_trade("Нулевая волатильность относительно VWAP (флэт).")
    if distance <= 0:
        return no_trade("Цена ниже VWAP.")

    pullback_threshold = mean_abs_deviation * pullback_tolerance_factor
    max_extension = mean_abs_deviation * max_extension_factor

    if distance > max_extension:
        return no_trade("Цена слишком далеко от VWAP — вход в погоне запрещён.")
    if distance > pullback_threshold:
        return no_trade("Отката к VWAP не произошло (кандидатский порог).")
    if last_close <= previous_close:
        return no_trade("Нет подтверждающей свечи разворота отката.")

    if require_volume_confirmation:
        volumes = market.volumes
        if len(volumes) >= 20:
            average_volume = sum(volumes[-20:]) / 20
            if average_volume > 0 and volumes[-1] < average_volume:
                return no_trade("Объём подтверждающей свечи ниже среднего.")

    session_start = vwap_data["start_index"]
    recent_lows = lows[max(session_start, len(lows) - 6):]
    if not recent_lows:
        return no_trade("Нет данных для структурного стопа.")

    structural_low = min(recent_lows)
    stop = structural_low - atr * VWAPTrendPullbackStrategy.STOP_BUFFER_ATR_FACTOR
    entry = last_close
    risk_distance = entry - stop

    if risk_distance <= 0:
        return no_trade("Некорректная дистанция до стопа.")

    tp1 = entry + risk_distance * MIN_RISK_REWARD
    tp2 = entry + risk_distance * 3

    return {
        "approved": True, "strategy": name, "direction": "LONG",
        "trade_plan": {
            "entry": entry, "stop_loss": stop,
            "take_profit": {"tp1": tp1, "tp2": tp2, "risk_reward": f"1:{MIN_RISK_REWARD:.0f} / 1:3"},
            "risk_reward": f"1:{MIN_RISK_REWARD:.0f} / 1:3",
        },
        "confidence": round(min(1.0, pullback_threshold / max(distance, 1e-9)), 3),
        "reason": "Бычий тренд + откат к session VWAP (кандидатский порог) + подтверждение.",
        "metadata": {"session": vwap_data["session"], "vwap": vwap_data["vwap"]},
    }


class VWAPTighterPullback:
    """Кандидат: требует более глубокий (ближе к VWAP) откат, чем baseline (0.75)."""
    NAME = "VWAP_TIGHTER_PULLBACK"

    @staticmethod
    def generate(context, apply_filters=True):
        return _vwap_candidate(context, VWAPTighterPullback.NAME, pullback_tolerance_factor=0.4,
                                apply_filters=apply_filters)


class VWAPWiderPullback:
    """Кандидат: допускает более широкий откат, чем baseline (0.75)."""
    NAME = "VWAP_WIDER_PULLBACK"

    @staticmethod
    def generate(context, apply_filters=True):
        return _vwap_candidate(context, VWAPWiderPullback.NAME, pullback_tolerance_factor=1.2,
                                apply_filters=apply_filters)


class VWAPWithVolumeConfirmation:
    """Кандидат: baseline-порог отката + требование объёма на свече подтверждения."""
    NAME = "VWAP_VOLUME_CONFIRMATION"

    @staticmethod
    def generate(context, apply_filters=True):
        return _vwap_candidate(context, VWAPWithVolumeConfirmation.NAME, pullback_tolerance_factor=0.75,
                                require_volume_confirmation=True, apply_filters=apply_filters)
