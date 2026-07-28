"""
Канонический контракт selected_trade.

ЗАЧЕМ. Раньше каждый шаг конвейера читал `context.strategy["selected_trade"]`
как сырой словарь: сам проверял тип, сам доставал поля, сам выводил
направление из сигнала. Отсюда два класса дефектов:

  1. Расхождение семантики `side`. Старая конвенция клала в это поле
     ТОРГОВЫЙ СИГНАЛ (BUY/SELL), текущая — НАПРАВЛЕНИЕ ПОЗИЦИИ
     (LONG/SHORT). PaperPositionManager отстал от миграции и сравнивал
     side с "BUY", из-за чего отвергал каждый валидный лонг. Модель
     закрывает это тем, что `side` больше не хранится и не передаётся —
     он ВЫЧИСЛЯЕТСЯ из сигнала в одном месте.

  2. Дублирование валидации. Одинаковые проверки жили в четырёх шагах и
     расходились в формулировках и строгости.

Модель намеренно СТРОГАЯ: неизвестный сигнал, нечисловой уровень или
отсутствующее поле — это ошибка, а не повод подставить умолчание.
Permissive-обработка здесь опаснее падения: молча принятый мусор
превращается в сделку.

Уровни (entry/stop/цели) необязательны, и это не послабление: координатор
штатно возвращает selected_trade без уровней для стратегии EMA — их
рассчитывает TradePlanStep из цены и ATR. Отсутствие уровней и НУЛЕВЫЕ
уровни — разные вещи, поэтому None здесь допустим, а 0.0 — нет.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


BUY = "BUY"
SELL = "SELL"
NO_TRADE = "NO TRADE"

LONG = "LONG"
SHORT = "SHORT"
NONE = "NONE"

ALLOWED_SIGNALS = (BUY, SELL, NO_TRADE)

LEVEL_FIELDS = (
    "entry",
    "stop",
    "take_profit_1",
    "take_profit_2",
)


class SelectedTradeError(ValueError):
    """Некорректный selected_trade. Осознанно не подавляется."""


def side_for_signal(signal: Any) -> str:
    """
    ЕДИНСТВЕННОЕ место, где направление выводится из сигнала.

    Раньше эта же формула была скопирована в RiskStep, TradePlanStep,
    DecisionStep и PaperExecutionStep. Любое расхождение между копиями
    переворачивало знак сделки.
    """
    if signal == BUY:
        return LONG

    if signal == SELL:
        return SHORT

    return NONE


def _optional_level(value: Any, field: str) -> float | None:
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectedTradeError(
            f"selected_trade.{field} must be a number or None, "
            f"got {value!r}"
        )

    number = float(value)

    if not math.isfinite(number):
        raise SelectedTradeError(
            f"selected_trade.{field} must be finite, got {value!r}"
        )

    return number


@dataclass(frozen=True)
class SelectedTrade:
    """Выбранная координатором сделка. Неизменяемая."""

    strategy: str
    signal: str
    entry: float | None = None
    stop: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    risk_reward: str = NO_TRADE
    reason: str | None = None
    real_order_sent: bool = False

    # ---------------------------------------------------------------- api

    @property
    def side(self) -> str:
        """Направление позиции. Выводится, а не хранится."""
        return side_for_signal(self.signal)

    @property
    def is_tradable(self) -> bool:
        return self.signal in (BUY, SELL)

    @property
    def has_levels(self) -> bool:
        return all(
            getattr(self, field) is not None
            for field in LEVEL_FIELDS
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "signal": self.signal,
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "risk_reward": self.risk_reward,
            "reason": self.reason,
            "real_order_sent": False,
        }

    # ------------------------------------------------------------ parsing

    @classmethod
    def from_mapping(cls, raw: Any) -> "SelectedTrade":
        if not isinstance(raw, Mapping):
            raise SelectedTradeError(
                f"selected_trade must be a mapping, got {type(raw).__name__}"
            )

        signal = raw.get("signal")

        if signal not in ALLOWED_SIGNALS:
            raise SelectedTradeError(
                f"selected_trade.signal must be one of {ALLOWED_SIGNALS}, "
                f"got {signal!r}"
            )

        strategy = raw.get("strategy", NONE)

        if not isinstance(strategy, str) or not strategy:
            raise SelectedTradeError(
                f"selected_trade.strategy must be a non-empty string, "
                f"got {strategy!r}"
            )

        levels = {
            field: _optional_level(raw.get(field), field)
            for field in LEVEL_FIELDS
        }

        risk_reward = raw.get("risk_reward", NO_TRADE)

        if not isinstance(risk_reward, str):
            raise SelectedTradeError(
                f"selected_trade.risk_reward must be a string, "
                f"got {risk_reward!r}"
            )

        reason = raw.get("reason")

        if reason is not None and not isinstance(reason, str):
            raise SelectedTradeError(
                f"selected_trade.reason must be a string or None, "
                f"got {reason!r}"
            )

        # Реальные ордера невозможны ни при каком входном значении.
        return cls(
            strategy=strategy,
            signal=signal,
            risk_reward=risk_reward,
            reason=reason,
            real_order_sent=False,
            **levels,
        )

    @classmethod
    def from_context_strategy(cls, strategy: Any) -> "SelectedTrade":
        """
        Достаёт selected_trade из context.strategy.

        Строго: отсутствие selected_trade — ошибка, а не повод собрать
        его из чего попало. В рабочем конвейере StrategyCoordinatorStep
        всегда выполняется раньше потребителей (порядок зафиксирован в
        tests/test_bootstrap.py), поэтому его отсутствие означает
        сломанный конвейер.
        """
        if not isinstance(strategy, Mapping):
            raise SelectedTradeError(
                "context.strategy must be a mapping, got "
                f"{type(strategy).__name__}"
            )

        if "selected_trade" not in strategy:
            raise SelectedTradeError(
                "context.strategy is missing selected_trade — "
                "StrategyCoordinatorStep must run before this step"
            )

        return cls.from_mapping(strategy["selected_trade"])


def normalise_legacy_strategy(strategy: Mapping[str, Any]) -> SelectedTrade:
    """
    Адаптер контракта, существовавшего ДО StrategyCoordinatorStep.

    Тогда стратегия отдавала только `{"signal": ...}` без уровней. Ровно
    это координатор и сегодня возвращает для ветки EMA: сигнал есть,
    уровни считает TradePlanStep из цены и ATR. Поэтому перевод
    однозначен и НЕ требует выдумывать значения — уровни остаются None.

    Адаптер существует для миграции и внешних вызовов. Внутренние шаги
    конвейера им не пользуются: они требуют канонический контракт.
    """
    if not isinstance(strategy, Mapping):
        raise SelectedTradeError(
            f"legacy strategy must be a mapping, got {type(strategy).__name__}"
        )

    if "selected_trade" in strategy:
        return SelectedTrade.from_mapping(strategy["selected_trade"])

    return SelectedTrade.from_mapping(
        {
            "strategy": "EMA",
            "signal": strategy.get("signal"),
            "risk_reward": "1:2 / 1:3",
            "reason": "Normalised from legacy strategy contract",
        }
    )
