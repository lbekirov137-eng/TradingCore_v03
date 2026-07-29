"""
Общий контракт стратегий paper-контура.

ГЛАВНОЕ СВОЙСТВО: доступ к будущим свечам структурно невозможен.

Стратегия получает не список свечей, а CandleWindow — представление,
которое физически не отдаёт ничего правее текущего индекса. Это сильнее
соглашения «не смотри вперёд»: даже ошибочный `candles[i + 1]` внутри
стратегии поднимет LookAheadError, а не вернёт будущее молча. Именно
такая ошибка обычно и не находится: результат становится лучше, а не
падает, поэтому она выглядит как удача, а не как баг.

Прочие инварианты:
  - решения только по ЗАКРЫТОЙ свече;
  - вывод детерминирован: одни и те же свечи дают один и тот же результат
    (никакого времени, случайности и глобального состояния);
  - только LONG;
  - конфигурация неизменяема (frozen dataclass);
  - никаких обращений к бирже, файлам и сети;
  - никаких побочных эффектов и глобального изменяемого состояния.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence


NO_TRADE = "NO_TRADE"
BUY = "BUY"
LONG = "LONG"


class LookAheadError(RuntimeError):
    """
    Попытка прочитать свечу правее текущей.

    Намеренно RuntimeError, а не тихий None: утечка будущего улучшает
    результат, поэтому обязана падать громко.
    """


class StrategyContractError(ValueError):
    """Некорректный вход стратегии. Осознанно не подавляется."""


@dataclass(frozen=True)
class Candle:
    """Одна ЗАКРЫТАЯ свеча."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)

            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise StrategyContractError(f"Candle.{name} must be a number")

            if not math.isfinite(float(value)):
                raise StrategyContractError(f"Candle.{name} must be finite")

            if float(value) <= 0:
                raise StrategyContractError(f"Candle.{name} must be > 0")

        if self.low > self.high:
            raise StrategyContractError("Candle.low must not exceed high")

        if not (self.low <= self.open <= self.high):
            raise StrategyContractError("Candle.open must lie within low..high")

        if not (self.low <= self.close <= self.high):
            raise StrategyContractError("Candle.close must lie within low..high")

        if not isinstance(self.volume, (int, float)) or self.volume < 0:
            raise StrategyContractError("Candle.volume must be >= 0")


class CandleWindow:
    """
    Свечи, доступные ПО СОСТОЯНИЮ НА текущий индекс.

    index — позиция последней ЗАКРЫТОЙ свечи. Всё правее недоступно.
    Отрицательные индексы разрешены и означают отсчёт от текущей свечи
    назад (-1 — предыдущая), потому что это естественный способ смотреть
    в прошлое и он не может случайно уехать в будущее.
    """

    __slots__ = ("_candles", "_index")

    def __init__(self, candles: Sequence[Candle], index: int) -> None:
        if index < 0 or index >= len(candles):
            raise StrategyContractError(
                f"index {index} is outside the candle range"
            )

        self._candles = candles
        self._index = index

    @property
    def index(self) -> int:
        return self._index

    @property
    def current(self) -> Candle:
        return self._candles[self._index]

    def __len__(self) -> int:
        """Длина = число доступных свечей, а не длина исходного списка."""
        return self._index + 1

    def __getitem__(self, position: int) -> Candle:
        if position < 0:
            absolute = self._index + 1 + position
        else:
            absolute = position

        if absolute > self._index:
            raise LookAheadError(
                f"strategy tried to read candle {absolute} while the last "
                f"closed candle is {self._index}: future data is not available"
            )

        if absolute < 0:
            raise StrategyContractError(f"candle {position} is before history")

        return self._candles[absolute]

    def closes(self, count: int) -> list[float]:
        """Последние count закрытий, старые -> новые."""
        start = max(0, self._index + 1 - count)

        return [candle.close for candle in self._candles[start : self._index + 1]]

    def slice(self, count: int) -> list[Candle]:
        start = max(0, self._index + 1 - count)

        return list(self._candles[start : self._index + 1])


@dataclass(frozen=True)
class StrategyConfig:
    """
    Неизменяемые параметры. Значения — из Strategy Implementation Contract.

    Frozen намеренно: параметр, изменяемый в рантайме, обесценивает всю
    накопленную статистику и открывает дверь подгонке под последние сделки.
    """

    atr_period: int = 14
    fast_ema: int = 20
    slow_ema: int = 50
    warmup_bars: int = 60
    structure_lookback: int = 20
    min_structure_confirmations: int = 2

    # Границы «боковика» из config/adaptive_orb.py (ATR_LOW / ATR_HIGH).
    atr_percent_min: float = 0.8
    atr_percent_max: float = 1.5

    vwap_zone_atr: float = 0.5
    retest_tolerance_atr: float = 0.25
    opening_range_minutes: int = 30

    max_trades_per_session: int = 1
    min_risk_reward: float = 2.0

    # stop = 1*ATR, TP1 = 2*ATR, TP2 = 3*ATR (api/trade_plan.py)
    stop_atr_multiple: float = 1.0
    tp1_atr_multiple: float = 2.0
    tp2_atr_multiple: float = 3.0

    def fingerprint(self) -> str:
        """
        Хеш параметров для validation.

        Нужен, чтобы «те же параметры» можно было доказать, а не
        утверждать: смена любого значения меняет отпечаток, и результат,
        полученный на других параметрах, нельзя выдать за прежний.
        """
        import hashlib

        payload = "|".join(
            f"{name}={getattr(self, name)}"
            for name in sorted(self.__dataclass_fields__)
        )

        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class StrategyDecision:
    """
    Результат оценки одной закрытой свечи.

    NO_TRADE — полноценный результат с ПРИЧИНОЙ, а не пустота: без кода
    причины невозможно отличить «сетапа нет» от «стратегия сломана».
    """

    strategy_key: str
    version: str
    signal: str
    reason_code: str
    entry: float | None = None
    stop: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    risk_reward: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.signal == BUY

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_key": self.strategy_key,
            "version": self.version,
            "signal": self.signal,
            "side": LONG if self.is_trade else "NONE",
            "reason_code": self.reason_code,
            "entry": self.entry,
            "stop": self.stop,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "risk_reward": self.risk_reward,
            "diagnostics": dict(self.diagnostics),
            "real_order_sent": False,
        }


# ------------------------------------------------------------- индикаторы
#
# Считаются здесь, а не берутся из пайплайна: стратегия обязана быть
# самодостаточной и воспроизводимой на голом списке свечей, иначе её
# нельзя честно прогнать по истории.


def ema(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)

    # Инициализация простым средним первых period значений — стандартная
    # и детерминированная; рекурсия «от первого значения» дала бы разный
    # ответ при разной длине входа.
    current = sum(values[:period]) / period

    for value in values[period:]:
        current = (value - current) * multiplier + current

    return current


def atr(candles: Sequence[Candle], period: int) -> float | None:
    if len(candles) < period + 1:
        return None

    true_ranges: list[float] = []

    for position in range(1, len(candles)):
        candle = candles[position]
        previous_close = candles[position - 1].close

        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )

    window = true_ranges[-period:]

    if len(window) < period:
        return None

    return sum(window) / period


def session_vwap(candles: Sequence[Candle]) -> float | None:
    """
    VWAP по переданному срезу (срез = сессия).

    При нулевом суммарном объёме возвращает None, а не цену: VWAP без
    объёма не определён, и подстановка close выдала бы за VWAP то, чем он
    не является.
    """
    total_volume = 0.0
    total_value = 0.0

    for candle in candles:
        typical = (candle.high + candle.low + candle.close) / 3.0
        total_value += typical * candle.volume
        total_volume += candle.volume

    if total_volume <= 0:
        return None

    return total_value / total_volume


def swing_points(
    candles: Sequence[Candle],
    left: int = 2,
    right: int = 2,
) -> tuple[list[int], list[int]]:
    """
    Индексы подтверждённых swing high / swing low.

    Точка подтверждается только когда справа от неё есть `right` баров —
    поэтому последние `right` баров НИКОГДА не дают swing. Это не
    ограничение, а корректность: swing, объявленный на последней свече,
    был бы утечкой будущего.
    """
    highs: list[int] = []
    lows: list[int] = []

    for position in range(left, len(candles) - right):
        candle = candles[position]

        left_slice = candles[position - left : position]
        right_slice = candles[position + 1 : position + 1 + right]

        if all(candle.high > item.high for item in left_slice) and all(
            candle.high > item.high for item in right_slice
        ):
            highs.append(position)

        if all(candle.low < item.low for item in left_slice) and all(
            candle.low < item.low for item in right_slice
        ):
            lows.append(position)

    return highs, lows


class BaseStrategy:
    """
    Базовый контракт. Реализации переопределяют только _evaluate.

    build_plan централизует построение уровней и проверку R:R, чтобы три
    стратегии не разошлись в арифметике — расхождение здесь означало бы,
    что их результаты несравнимы.
    """

    strategy_key = "BASE"
    version = "0.0.0"

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    @property
    def required_warmup_bars(self) -> int:
        return self.config.warmup_bars

    # ------------------------------------------------------------- public

    def evaluate_closed_candle(
        self,
        candles: Sequence[Candle],
        index: int,
    ) -> StrategyDecision:
        """
        Оценивает ЗАКРЫТУЮ свечу под номером index.

        Свечи правее index недоступны — это обеспечено CandleWindow, а не
        дисциплиной реализации.
        """
        window = CandleWindow(candles, index)

        if len(window) < self.required_warmup_bars:
            return self.no_trade(
                "INSUFFICIENT_WARMUP",
                available=len(window),
                required=self.required_warmup_bars,
            )

        return self._evaluate(window)

    # ---------------------------------------------------------- overrides

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        raise NotImplementedError

    # ----------------------------------------------------------- helpers

    def no_trade(self, reason_code: str, **diagnostics: Any) -> StrategyDecision:
        return StrategyDecision(
            strategy_key=self.strategy_key,
            version=self.version,
            signal=NO_TRADE,
            reason_code=reason_code,
            diagnostics=diagnostics,
        )

    def build_plan(
        self,
        entry: float,
        atr_value: float,
        reason_code: str,
        **diagnostics: Any,
    ) -> StrategyDecision:
        """
        Строит LONG-план по проектной формуле и проверяет R:R.

        Если R:R ниже минимума — возвращается NO_TRADE, а не ослабленный
        план: цель, подогнанная под порог, делает порог бессмысленным.
        """
        config = self.config

        if atr_value <= 0:
            return self.no_trade("INVALID_ATR", atr=atr_value)

        stop = entry - config.stop_atr_multiple * atr_value
        take_profit_1 = entry + config.tp1_atr_multiple * atr_value
        take_profit_2 = entry + config.tp2_atr_multiple * atr_value

        if stop <= 0 or not (stop < entry < take_profit_1 < take_profit_2):
            return self.no_trade(
                "INVALID_LEVELS",
                entry=entry,
                stop=stop,
                take_profit_1=take_profit_1,
                take_profit_2=take_profit_2,
            )

        risk = entry - stop
        risk_reward = (take_profit_1 - entry) / risk

        if risk_reward < config.min_risk_reward:
            return self.no_trade(
                "RISK_REWARD_BELOW_MINIMUM",
                risk_reward=round(risk_reward, 4),
                minimum=config.min_risk_reward,
            )

        return StrategyDecision(
            strategy_key=self.strategy_key,
            version=self.version,
            signal=BUY,
            reason_code=reason_code,
            entry=round(entry, 2),
            stop=round(stop, 2),
            take_profit_1=round(take_profit_1, 2),
            take_profit_2=round(take_profit_2, 2),
            risk_reward=round(risk_reward, 4),
            diagnostics=diagnostics,
        )


def candles_from_arrays(payload: dict[str, Any]) -> list[Candle]:
    """
    Строит свечи из проектного формата data/BTCUSDT_5m*.json.

    Формат — «массивы по колонкам» (timestamps/opens/highs/...). Строки с
    несогласованной длиной или некорректными значениями ОТБРАСЫВАЮТСЯ, а
    не чинятся: молча исправленная свеча — это выдуманные данные.
    """
    timestamps = payload.get("timestamps") or []
    opens = payload.get("opens") or []
    highs = payload.get("highs") or []
    lows = payload.get("lows") or []
    closes = payload.get("closes") or []
    volumes = payload.get("volumes") or []

    size = min(
        len(timestamps), len(opens), len(highs), len(lows), len(closes), len(volumes)
    )

    candles: list[Candle] = []

    for position in range(size):
        try:
            candles.append(
                Candle(
                    open_time_ms=int(timestamps[position]),
                    open=float(opens[position]),
                    high=float(highs[position]),
                    low=float(lows[position]),
                    close=float(closes[position]),
                    volume=float(volumes[position]),
                )
            )
        except (StrategyContractError, TypeError, ValueError):
            continue

    # Хронологический порядок принудительно: несортированный вход иначе
    # дал бы индикаторы, посчитанные по перемешанному времени.
    candles.sort(key=lambda item: item.open_time_ms)

    return candles
