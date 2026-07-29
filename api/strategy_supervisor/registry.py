"""
Реестр РАЗРЕШЁННЫХ стратегий. Единственный источник того, что вообще
может быть выбрано супервизором.

ГЛАВНЫЙ ИНВАРИАНТ. Супервизор умеет только ВЫБИРАТЬ из этого списка. Он
не может придумать стратегию, переписать её правила или подкрутить
параметры — ни сам, ни «с помощью модели». Это не соглашение в
комментарии, а свойство типов:

  - StrategySpec — frozen dataclass, присваивание полю бросает исключение;
  - параметры хранятся кортежем пар и отдаются копией, поэтому мутация
    возвращённого словаря не трогает реестр;
  - в модуле нет ни одной функции, создающей или изменяющей спецификацию
    из внешних данных; всё содержимое реестра — литералы в коде.

Почему так строго. Стратегия, изменяемая в рабочем цикле, делает всю
накопленную статистику бессмысленной: выборка перестаёт относиться к
одному и тому же объекту. Плюс параметры, подогнанные под последние
убытки, — это ровно то переобучение, ради запрета которого реестр и
существует.

Изменение реестра — это правка кода, ревью и коммит. Так и задумано.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# --------------------------------------------------------------- статусы

CANDIDATE = "CANDIDATE"
PAPER_ACTIVE = "PAPER_ACTIVE"
PAUSED = "PAUSED"
REJECTED = "REJECTED"

ALLOWED_STATUSES = (CANDIDATE, PAPER_ACTIVE, PAUSED, REJECTED)


# -------------------------------------------------------------- режимы

TREND = "TREND"
RANGE = "RANGE"
BREAKOUT = "BREAKOUT"
VOLATILE = "VOLATILE"

ALLOWED_REGIMES = (TREND, RANGE, BREAKOUT, VOLATILE)


class StrategyRegistryError(ValueError):
    """Некорректная спецификация стратегии. Осознанно не подавляется."""


@dataclass(frozen=True)
class CostModel:
    """
    Издержки, с которыми стратегия обязана оцениваться.

    Хранится В спецификации, а не берётся из окружения на момент оценки:
    иначе сравнение двух стратегий могло бы пройти при разных тарифах и
    выбрать «лучшую» просто потому, что её мерили дешевле.
    """

    taker_fee_rate: float = 0.0010
    maker_fee_rate: float = 0.0010
    slippage_bps: float = 5.0
    slippage_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "taker_fee_rate": self.taker_fee_rate,
            "maker_fee_rate": self.maker_fee_rate,
            "slippage_bps": self.slippage_bps,
            "slippage_enabled": self.slippage_enabled,
        }


@dataclass(frozen=True)
class StrategySpec:
    """
    Неизменяемая спецификация стратегии.

    tradable=False — это НЕ отключённая стратегия, а осознанная политика
    «в этом режиме мы не торгуем». Она участвует в выборе наравне с
    остальными, потому что «ничего не делать» — допустимый и часто лучший
    исход, который обязан быть представлен явно, а не подразумеваться.
    """

    strategy_id: str
    name: str
    version: str
    status: str

    allowed_regimes: tuple[str, ...]
    entry_criteria: tuple[str, ...]
    exit_criteria: tuple[str, ...]

    min_risk_reward: float
    cost_model: CostModel = field(default_factory=CostModel)

    # Параметры — кортеж пар, а не словарь: dict внутри frozen dataclass
    # остаётся изменяемым, и «неизменяемый набор параметров» был бы
    # неправдой.
    parameters: tuple[tuple[str, Any], ...] = ()

    tradable: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in ALLOWED_STATUSES:
            raise StrategyRegistryError(
                f"{self.strategy_id}: status must be one of "
                f"{ALLOWED_STATUSES}, got {self.status!r}"
            )

        if not self.allowed_regimes:
            raise StrategyRegistryError(
                f"{self.strategy_id}: allowed_regimes must not be empty"
            )

        for regime in self.allowed_regimes:
            if regime not in ALLOWED_REGIMES:
                raise StrategyRegistryError(
                    f"{self.strategy_id}: unknown regime {regime!r}"
                )

        if self.tradable and self.min_risk_reward < 1.0:
            raise StrategyRegistryError(
                f"{self.strategy_id}: min_risk_reward must be >= 1.0 "
                f"for a tradable strategy, got {self.min_risk_reward}"
            )

        if self.tradable and not self.entry_criteria:
            raise StrategyRegistryError(
                f"{self.strategy_id}: tradable strategy needs entry criteria"
            )

    @property
    def params(self) -> dict[str, Any]:
        """Копия параметров. Мутация результата реестр не затрагивает."""
        return dict(self.parameters)

    @property
    def key(self) -> str:
        """Идентификатор с версией: разные версии — разные объекты оценки."""
        return f"{self.strategy_id}@{self.version}"

    def allows_regime(self, regime: Any) -> bool:
        if not isinstance(regime, str):
            return False

        return regime.strip().upper() in self.allowed_regimes

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "key": self.key,
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "tradable": self.tradable,
            "allowed_regimes": list(self.allowed_regimes),
            "entry_criteria": list(self.entry_criteria),
            "exit_criteria": list(self.exit_criteria),
            "min_risk_reward": self.min_risk_reward,
            "cost_model": self.cost_model.to_dict(),
            "parameters": self.params,
            "notes": self.notes,
        }


# --------------------------------------------------------------- реестр
#
# Все записи — литералы. Ни одна не собирается из окружения, из журнала
# или из ответа модели.

SESSION_VWAP_TREND_PULLBACK = StrategySpec(
    strategy_id="SESSION_VWAP_TREND_PULLBACK",
    name="Session VWAP Trend Pullback",
    version="1.0.0",
    status=CANDIDATE,
    allowed_regimes=(TREND,),
    entry_criteria=(
        "Session VWAP is rising and price holds above it",
        "Pullback touches VWAP band without closing below it",
        "Entry on reclaim of the pullback high",
    ),
    exit_criteria=(
        "Stop below the pullback low",
        "TP1 at 1R, TP2 at 2R",
        "Time stop at session close",
    ),
    min_risk_reward=2.0,
    parameters=(
        ("vwap_band_atr_multiple", 0.5),
        ("min_trend_strength", 0.4),
        ("time_stop_minutes", 240),
    ),
    notes=(
        "Наследуемая VWAP-реализация помечена RESEARCH_ONLY "
        "(backtest: 98 сделок, net -128.37, profit factor 0.092). "
        "Здесь она зарегистрирована как КАНДИДАТ и обязана заново пройти "
        "OOS-гейты — прошлый отрицательный результат не даёт ей допуска."
    ),
)

LONDON_SESSION_BREAKOUT_RETEST = StrategySpec(
    strategy_id="LONDON_SESSION_BREAKOUT_RETEST",
    name="London Session Breakout + Retest",
    version="1.0.0",
    status=CANDIDATE,
    allowed_regimes=(BREAKOUT, TREND),
    entry_criteria=(
        "London session opening range is established",
        "Price breaks the range and closes outside it",
        "Entry only on a successful retest that holds",
    ),
    exit_criteria=(
        "Stop on the far side of the retest candle",
        "TP1 at 1R, TP2 at 2R",
        "Invalidate if price re-enters the range",
    ),
    min_risk_reward=2.0,
    parameters=(
        ("session", "LONDON"),
        ("opening_range_minutes", 30),
        ("retest_tolerance_atr", 0.25),
        ("max_trades_per_session", 1),
    ),
)

ORB_0930_RETEST = StrategySpec(
    strategy_id="ORB_0930_RETEST",
    name="ORB 09:30-09:35 + Retest",
    version="1.0.0",
    status=CANDIDATE,
    allowed_regimes=(BREAKOUT,),
    entry_criteria=(
        "Opening range built from 09:30-09:35 candles",
        "Break of the opening range high",
        "Entry only on retest that holds above the range",
    ),
    exit_criteria=(
        "Stop below the opening range low",
        "TP1 at 1R, TP2 at 2R",
        "Time stop 120 minutes after the open",
    ),
    min_risk_reward=2.0,
    parameters=(
        ("range_start", "09:30"),
        ("range_end", "09:35"),
        ("retest_tolerance_atr", 0.25),
        ("time_stop_minutes", 120),
    ),
    notes=(
        "Наследуемая ORB-реализация помечена RESEARCH_ONLY "
        "(208 сделок, net -176.54, profit factor 0.205, "
        "walk-forward consistency 0.0%). Допуск только через OOS-гейты."
    ),
)

TREND_PULLBACK_EMA_STRUCTURE = StrategySpec(
    strategy_id="TREND_PULLBACK_EMA_STRUCTURE",
    name="Trend Pullback EMA / Market Structure",
    version="1.0.0",
    status=CANDIDATE,
    allowed_regimes=(TREND,),
    entry_criteria=(
        "EMA stack aligned with the higher timeframe trend",
        "Market structure shows a higher low",
        "Entry on reclaim of the prior swing high",
    ),
    exit_criteria=(
        "Stop below the confirmed higher low",
        "TP1 at 1R, TP2 at 2R",
        "Exit on structure break against the position",
    ),
    min_risk_reward=2.0,
    parameters=(
        ("fast_ema", 20),
        ("slow_ema", 50),
        ("min_structure_confirmations", 2),
    ),
)

RANGE_NO_TRADE_POLICY = StrategySpec(
    strategy_id="RANGE_NO_TRADE_POLICY",
    name="RANGE regime: NO_TRADE by default",
    version="1.0.0",
    status=CANDIDATE,
    allowed_regimes=(RANGE,),
    entry_criteria=(),
    exit_criteria=("No position is ever opened",),
    min_risk_reward=0.0,
    tradable=False,
    notes=(
        "Политика, а не стратегия: в RANGE по умолчанию не торгуем. "
        "Зарегистрирована ЯВНО, потому что «ничего не делать» — "
        "допустимый исход выбора, и он обязан быть представлен объектом, "
        "а не отсутствием объекта. Это же даёт безопасный fallback, "
        "когда ни один кандидат не прошёл гейты."
    ),
)


STRATEGY_REGISTRY: tuple[StrategySpec, ...] = (
    SESSION_VWAP_TREND_PULLBACK,
    LONDON_SESSION_BREAKOUT_RETEST,
    ORB_0930_RETEST,
    TREND_PULLBACK_EMA_STRUCTURE,
    RANGE_NO_TRADE_POLICY,
)

# Безопасный режим по умолчанию: пока ни одна стратегия не подтверждена
# OOS-выборкой, активна политика «не торгуем».
DEFAULT_STRATEGY_ID = RANGE_NO_TRADE_POLICY.strategy_id


def all_strategies() -> tuple[StrategySpec, ...]:
    return STRATEGY_REGISTRY


def get_strategy(strategy_id: str) -> StrategySpec:
    for spec in STRATEGY_REGISTRY:
        if spec.strategy_id == strategy_id:
            return spec

    raise StrategyRegistryError(
        f"Strategy {strategy_id!r} is not registered. The supervisor may "
        "only select from the registry; inventing a strategy at runtime "
        "is not possible by design."
    )


def is_registered(strategy_id: Any) -> bool:
    return isinstance(strategy_id, str) and any(
        spec.strategy_id == strategy_id for spec in STRATEGY_REGISTRY
    )


def registry_snapshot() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in STRATEGY_REGISTRY]


def tradable_candidates() -> tuple[StrategySpec, ...]:
    return tuple(spec for spec in STRATEGY_REGISTRY if spec.tradable)
