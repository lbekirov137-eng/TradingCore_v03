"""
SESSION_VWAP_RANGE_LOW_VOL@3.0.0 — исследовательская версия.

ПОЧЕМУ ЭТО НОВАЯ СТРАТЕГИЯ, А НЕ НАСТРОЙКА @2.0.0.
Фильтр режима выведен из OOS-анализа предыдущей выборки: там обнаружилось,
что VWAP прибылен после полных издержек только в RANGE + LOW_VOL
(PF 2.87, win 62.5%, +0.82R). Применить это к @2.0.0 и заявить прежние
результаты означало бы подгонку под уже увиденные данные. Поэтому:
новый strategy_key, новый parameter_hash, новый sample_id, отдельная
история валидации, и @2.0.0 остаётся нетронутой.

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ, ЗАФИКСИРОВАННОЕ В КОДЕ.
Пороги ниже помечены derived_from_prior_analysis=True. Они НЕ подбирались
заново, но они и не независимы: их подсказала прошлая выборка. Поэтому
единственная валидная проверка — на периоде, который тот анализ не видел
(2021-07..2023-07), и результат на нём считается главным.

Спецификация зафиксирована ДО прогона и после него не менялась.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.strategy_engine.strategies.contracts import (
    CandleWindow,
    StrategyDecision,
    atr,
    ema,
)
from api.strategy_engine.strategies.v2_structural import (
    SessionVwapTrendPullbackV2,
    StructuralConfig,
)


@dataclass(frozen=True)
class RangeLowVolConfig(StructuralConfig):
    """
    Конфигурация @3.0.0. Отдельный класс -> отдельный parameter_hash.

    Пороги совпадают с границами бакетов прошлого анализа НАМЕРЕННО: брать
    другие значения означало бы подбирать их заново, а это худший вариант.
    Так они хотя бы заданы до прогона и одним решением.
    """

    # RANGE: разрыв EMA20/EMA50 в процентах цены ниже порога.
    # Источник: граница бакета RANGE в regime lab. DEFAULT_NOT_OPTIMIZED.
    range_ema_gap_max_pct: float = 0.40

    # LOW_VOL: ATR в процентах цены ниже порога.
    # Источник: граница бакета LOW_VOL в regime lab. DEFAULT_NOT_OPTIMIZED.
    low_vol_atr_max_pct: float = 0.45

    # Нижняя граница ATR: совсем мёртвый рынок не даёт ни движения, ни
    # структуры. Консервативно, задано до прогона.
    low_vol_atr_min_pct: float = 0.10

    derived_from_prior_analysis: bool = True


class SessionVwapRangeLowVol(SessionVwapTrendPullbackV2):
    """
    VWAP-откат, ограниченный режимом RANGE + LOW_VOL.

    Наследует ВСЮ логику входа @2.0.0 без изменений и добавляет ровно один
    фильтр. Это сделано специально: если бы одновременно поменялась и
    логика входа, невозможно было бы сказать, что именно повлияло на
    результат.

    ВАЖНО: фильтр режима у @2.0.0 работал в обратную сторону — там ATR
    ниже 0.8% отвергался как «боковик». Здесь боковик, наоборот, является
    условием входа, поэтому проверка родителя обойдена намеренно и это
    единственное отличие в потоке решений.
    """

    strategy_key = "SESSION_VWAP_RANGE_LOW_VOL"
    version = "3.0.0"

    def __init__(self, config=None, cost_gate_config=None) -> None:
        super().__init__(config or RangeLowVolConfig(), cost_gate_config)

    def _regime_ok(self, window: CandleWindow):
        """
        Классификация режима по ЗАКРЫТЫМ свечам.

        Возвращает (ok, диагностика). Никаких будущих данных: и ATR, и EMA
        считаются по истории до текущей свечи включительно.
        """
        config = self.config
        current = window.current

        history = window.slice(config.warmup_bars)
        atr_value = atr(history, config.atr_period)

        if atr_value is None or atr_value <= 0:
            return False, {"regime": "ATR_UNAVAILABLE"}

        atr_pct = atr_value / current.close * 100.0

        closes = window.closes(config.warmup_bars)
        fast = ema(closes, config.fast_ema)
        slow = ema(closes, config.slow_ema)

        if fast is None or slow is None:
            return False, {"regime": "EMA_UNAVAILABLE"}

        ema_gap_pct = (fast - slow) / current.close * 100.0

        diagnostics = {
            "atr_percent": round(atr_pct, 4),
            "ema_gap_percent": round(ema_gap_pct, 4),
            "range_threshold": config.range_ema_gap_max_pct,
            "low_vol_threshold": config.low_vol_atr_max_pct,
        }

        if atr_pct > config.low_vol_atr_max_pct:
            return False, {**diagnostics, "regime": "NOT_LOW_VOL"}

        if atr_pct < config.low_vol_atr_min_pct:
            return False, {**diagnostics, "regime": "VOLATILITY_TOO_DEAD"}

        # RANGE: тренд не выражен НИ В ОДНУ сторону. abs() важен: сильный
        # нисходящий тренд — тоже не боковик, и лонг там не нужен.
        if abs(ema_gap_pct) > config.range_ema_gap_max_pct:
            return False, {**diagnostics, "regime": "NOT_RANGE"}

        return True, {**diagnostics, "regime": "RANGE_LOW_VOL"}

    def _evaluate_context(self, window, context_candles) -> StrategyDecision:
        ok, regime = self._regime_ok(window)

        if not ok:
            return self.no_trade("REGIME_NOT_ELIGIBLE", **regime)

        decision = self._evaluate_vwap_core(window, context_candles, regime)

        return decision

    def _evaluate_vwap_core(self, window, context_candles, regime):
        """
        Логика входа @2.0.0 без её собственного режимного фильтра.

        Родительский _evaluate_context отвергал ATR ниже 0.8% как боковик;
        здесь боковик — условие входа, поэтому вызывается та же
        последовательность проверок, но без противоречащего шага.
        """
        config = self.config
        current = window.current

        history = window.slice(config.warmup_bars)
        atr_value = atr(history, config.atr_period)

        if atr_value is None or atr_value <= 0:
            return self.no_trade("ATR_UNAVAILABLE", **regime)

        bullish, context = self.context_is_bullish(window, context_candles)

        if not bullish:
            return self.no_trade("CONTEXT_NOT_BULLISH", **regime, **context)

        from api.strategy_engine.strategies.contracts import session_vwap

        session = self._session_slice(window)
        vwap = session_vwap(session)

        if vwap is None:
            return self.no_trade("VWAP_UNAVAILABLE", **regime, **context)

        if current.close < vwap:
            return self.no_trade(
                "PRICE_BELOW_VWAP", vwap=round(vwap, 2), **regime, **context
            )

        zone = config.vwap_zone_atr * atr_value

        if not any(candle.low <= vwap + zone for candle in session[:-1]):
            return self.no_trade("NO_CONFIRMED_PULLBACK", **regime, **context)

        if current.close <= current.open:
            return self.no_trade(
                "CONFIRMATION_CANDLE_NOT_BULLISH", **regime, **context
            )

        stop, status, structure = self.structural_stop(window, atr_value)

        if stop is None:
            return self.no_trade(status, **regime, **context)

        return self.finalise(
            entry=current.close,
            stop=stop,
            atr_value=atr_value,
            reason_code="VWAP_RANGE_LOW_VOL_CONFIRMED",
            vwap=round(vwap, 2),
            **structure,
            **regime,
            **context,
        )
