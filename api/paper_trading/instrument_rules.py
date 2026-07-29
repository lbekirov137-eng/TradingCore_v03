"""
Правила округления цены и количества по инструменту.

ЗАЧЕМ. Уровни округлялись до ДВУХ знаков независимо от инструмента. Для
BTC (~$68 000) это безобидно, для TRX (~$0.1) — разрушительно: entry и
stop схлопываются в одно значение, ширина риска становится нулевой, и
сигнал исчезает. Замер: 96 из 116 сигналов validation-набора уничтожены
именно так, из них 89 у TRX и все 2 у ADA.

Настоящий шаг цены задаётся биржей (PRICE_FILTER.tickSize), а не ценой
инструмента. Выводить точность из величины цены — та же ошибка в новой
обёртке: два инструмента с одинаковой ценой могут иметь разный тик.
Поэтому правила берутся из метаданных биржи и фиксируются как fixtures.

Модуль ничего не исполняет и не ходит в сеть: значения зафиксированы
снимком exchangeInfo и пригодны для offline-тестов.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any


# Причины отказа геометрии ордера.
LEVELS_COLLAPSED = "LEVELS_COLLAPSED"
STOP_DISTANCE_ZERO = "STOP_DISTANCE_ZERO"
TARGET_COLLAPSED = "TARGET_COLLAPSED"
QUANTITY_ZERO = "QUANTITY_ZERO"
BELOW_MIN_QUANTITY = "BELOW_MIN_QUANTITY"
BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
RR_DISTORTED_BY_ROUNDING = "RR_DISTORTED_BY_ROUNDING"
GEOMETRY_OK = "GEOMETRY_OK"

# Допустимое искажение R:R округлением. Округление всегда что-то сдвигает;
# вопрос в том, насколько. 2% — консервативный предел: за ним заявленный
# R:R перестаёт описывать реальную сделку.
MAX_RR_DISTORTION = 0.02


@dataclass(frozen=True)
class InstrumentRules:
    """Неизменяемые правила инструмента. Источник — exchangeInfo."""

    symbol: str
    tick_size: float
    step_size: float
    min_quantity: float
    min_notional: float
    source: str = "binance exchangeInfo snapshot 2026-07-29"

    @property
    def price_precision(self) -> int:
        return _decimals(self.tick_size)

    @property
    def quantity_precision(self) -> int:
        return _decimals(self.step_size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_size": self.tick_size,
            "step_size": self.step_size,
            "price_precision": self.price_precision,
            "quantity_precision": self.quantity_precision,
            "min_quantity": self.min_quantity,
            "min_notional": self.min_notional,
            "source": self.source,
        }


def _decimals(step: float) -> int:
    """Число знаков после запятой, подразумеваемое шагом."""
    d = Decimal(str(step)).normalize()
    exponent = d.as_tuple().exponent

    return max(0, -int(exponent))


# Снимок реальных фильтров Binance spot (PRICE_FILTER / LOT_SIZE /
# NOTIONAL), снят 2026-07-29. Зафиксирован как fixture: тесты не должны
# зависеть от сети, а результаты валидации — от дня прогона.
INSTRUMENT_RULES: dict[str, InstrumentRules] = {
    "BTCUSDT": InstrumentRules("BTCUSDT", 0.01, 1e-05, 1e-05, 5.0),
    "ETHUSDT": InstrumentRules("ETHUSDT", 0.01, 0.0001, 0.0001, 5.0),
    "BNBUSDT": InstrumentRules("BNBUSDT", 0.01, 0.001, 0.001, 5.0),
    "LTCUSDT": InstrumentRules("LTCUSDT", 0.01, 0.001, 0.001, 5.0),
    "SOLUSDT": InstrumentRules("SOLUSDT", 0.01, 0.001, 0.001, 5.0),
    "ADAUSDT": InstrumentRules("ADAUSDT", 0.0001, 0.1, 0.1, 5.0),
    "XRPUSDT": InstrumentRules("XRPUSDT", 0.0001, 0.1, 0.1, 5.0),
    "TRXUSDT": InstrumentRules("TRXUSDT", 0.0001, 0.1, 0.1, 5.0),
    "BCHUSDT": InstrumentRules("BCHUSDT", 0.01, 0.00001, 0.00001, 5.0),
}

# Fallback для инструмента без снимка. НАМЕРЕННО консервативен по цене
# (мелкий тик безопаснее крупного: он не схлопывает уровни), но требует
# явного признания, что правила неизвестны.
FALLBACK_RULES = InstrumentRules(
    symbol="UNKNOWN", tick_size=1e-08, step_size=1e-08,
    min_quantity=0.0, min_notional=5.0,
    source="FALLBACK - exchange rules unknown for this symbol",
)


def rules_for(symbol: str) -> InstrumentRules:
    return INSTRUMENT_RULES.get(symbol, FALLBACK_RULES)


def round_price_to_tick(
    price: float,
    rules: InstrumentRules,
    mode: str = "nearest",
) -> float:
    """
    Приводит цену к сетке тиков.

    mode="down" нужен для СТОПА длинной позиции: округление вниз делает
    стоп чуть дальше, то есть риск чуть больше заявленного. Округление
    вверх сделало бы риск МЕНЬШЕ обещанного, а это тихое нарушение лимита
    риска — ошибка в опасную сторону.
    """
    if rules.tick_size <= 0:
        return float(price)

    tick = Decimal(str(rules.tick_size))
    value = Decimal(str(price))

    if mode == "down":
        units = (value / tick).to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        units = (value / tick).to_integral_value(rounding=ROUND_DOWN)
        if units * tick < value:
            units += 1
    else:
        units = (value / tick).to_integral_value(rounding=ROUND_HALF_UP)

    return float(units * tick)


def round_quantity_to_step(quantity: float, rules: InstrumentRules) -> float:
    """
    Приводит количество к шагу лота, ВСЕГДА вниз.

    Вниз, потому что округление вверх увеличило бы позицию и, значит,
    фактический риск сверх заявленных 0.1%.
    """
    if rules.step_size <= 0:
        return float(quantity)

    step = Decimal(str(rules.step_size))
    value = Decimal(str(quantity))

    units = (value / step).to_integral_value(rounding=ROUND_DOWN)

    return float(units * step)


def validate_order_geometry(
    *,
    entry: float,
    stop: float,
    take_profit_1: float,
    take_profit_2: float,
    quantity: float,
    rules: InstrumentRules,
    intended_rr: float | None = None,
    max_rr_distortion: float = MAX_RR_DISTORTION,
) -> dict[str, Any]:
    """
    Проверяет, что после округления сделка осталась исполнимой сделкой.

    Каждая причина отказа называется явно: молчаливое исчезновение сигнала
    и есть тот дефект, ради которого написан этот модуль.
    """
    detail: dict[str, Any] = {
        "symbol": rules.symbol,
        "entry": entry, "stop": stop,
        "take_profit_1": take_profit_1, "take_profit_2": take_profit_2,
        "quantity": quantity,
        "notional": round(quantity * entry, 8),
        "tick_size": rules.tick_size, "step_size": rules.step_size,
    }

    if entry == stop:
        return {"valid": False, "reason": LEVELS_COLLAPSED, **detail}

    stop_distance = entry - stop

    if stop_distance <= 0:
        return {"valid": False, "reason": STOP_DISTANCE_ZERO, **detail}

    if take_profit_1 <= entry or take_profit_2 <= take_profit_1:
        return {"valid": False, "reason": TARGET_COLLAPSED, **detail}

    if quantity <= 0:
        return {"valid": False, "reason": QUANTITY_ZERO, **detail}

    if quantity < rules.min_quantity:
        return {"valid": False, "reason": BELOW_MIN_QUANTITY, **detail}

    notional = quantity * entry

    if notional < rules.min_notional:
        return {"valid": False, "reason": BELOW_MIN_NOTIONAL, **detail}

    realised_rr = (take_profit_2 - entry) / stop_distance
    detail["realised_rr"] = round(realised_rr, 6)

    if intended_rr is not None and intended_rr > 0:
        distortion = abs(realised_rr - intended_rr) / intended_rr
        detail["rr_distortion"] = round(distortion, 6)

        if distortion > max_rr_distortion:
            return {"valid": False, "reason": RR_DISTORTED_BY_ROUNDING, **detail}

    return {"valid": True, "reason": GEOMETRY_OK, **detail}


def rules_snapshot() -> list[dict[str, Any]]:
    return [r.to_dict() for r in INSTRUMENT_RULES.values()]
