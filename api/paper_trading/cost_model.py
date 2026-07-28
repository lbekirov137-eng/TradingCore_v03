"""
Модель торговых издержек для paper-контура.

Зачем она нужна. Пока издержки не моделировались, paper-результат был
систематически завышен: сделка «зарабатывала» полную разницу цен, хотя на
реальной бирже её съедали бы комиссия за вход, комиссия за выход и
проскальзывание. Для стратегии с целью 3R и риском 0.1% капитала это
искажение сопоставимо с самим результатом, поэтому любые выводы о
прибыльности без него недостоверны.

Принципы:
  - тарифы НЕ зашиты в код: значения читаются из окружения, а константы
    в классе — лишь консервативный запасной вариант;
  - консервативность по умолчанию: обе стороны считаются тейкерными,
    проскальзывание включено и всегда играет ПРОТИВ сделки;
  - gross и net хранятся раздельно, net считается только после вычета
    всех издержек;
  - конфигурация снимается в журнал вместе со сделкой, чтобы результат
    можно было перепроверить задним числом;
  - проскальзывание отключаемо — это нужно тестам, проверяющим чистую
    арифметику, но выключение обязано быть явным.

Реальные ордера этот модуль не отправляет и отправлять не может.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any


MAKER = "MAKER"
TAKER = "TAKER"

LONG = "LONG"
SHORT = "SHORT"

# В репозитории сосуществуют две конвенции направления: старая записывала
# в поле "side" торговый СИГНАЛ (BUY/SELL), текущая — направление позиции
# (LONG/SHORT). Нормализация обязательна: если принять "BUY" за нечто
# отличное от LONG, знак результата перевернётся и убыток превратится в
# прибыль. Неизвестное значение трактуется как LONG, потому что режим
# исполнения — SPOT_LONG_ONLY, а не молча меняет знак.
_LONG_ALIASES = {LONG, "BUY"}
_SHORT_ALIASES = {SHORT, "SELL"}


def normalise_side(side: Any) -> str:
    value = str(side).strip().upper()

    if value in _SHORT_ALIASES:
        return SHORT

    return LONG

BASIS_POINT = 1.0 / 10_000.0


def _float_from_env(name: str, default: float) -> float:
    """
    Читает неотрицательное конечное число из окружения.

    Некорректное значение НЕ роняет расчёт и НЕ отключает издержки молча:
    берётся консервативный default, а подмена печатается. Тихо обнулить
    комиссию опаснее, чем взять слегка неточную.
    """
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    try:
        value = float(raw.strip())
    except ValueError:
        print(
            f"[COST MODEL] {name}={raw!r} is not a number; "
            f"falling back to {default}",
            flush=True,
        )
        return default

    if not math.isfinite(value) or value < 0:
        print(
            f"[COST MODEL] {name}={value} must be finite and >= 0; "
            f"falling back to {default}",
            flush=True,
        )
        return default

    return value


def _flag_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    return raw.strip().lower() in ("1", "true", "yes", "on")


def _liquidity_from_env(name: str, default: str) -> str:
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    value = raw.strip().upper()

    if value not in (MAKER, TAKER):
        print(
            f"[COST MODEL] {name}={raw!r} must be MAKER or TAKER; "
            f"falling back to {default}",
            flush=True,
        )
        return default

    return value


@dataclass(frozen=True)
class TradingCostConfig:
    """Конфигурация издержек. Неизменяемая — снимок пишется в журнал."""

    VERSION = "1.0.0"

    # Консервативные значения по умолчанию.
    #
    # 0.10% — намеренно пессимистичный тариф: он не ниже публичных
    # спотовых тарифов крупных бирж для аккаунта без скидок, поэтому
    # модель скорее занизит результат, чем завысит. Конкретные тарифы
    # конкретной биржи задаются переменными окружения.
    DEFAULT_MAKER_FEE_RATE = 0.0010
    DEFAULT_TAKER_FEE_RATE = 0.0010

    # 5 базисных пунктов = 0.05%. Для ликвидной пары вроде BTCUSDT это
    # осторожная, но не абсурдная оценка на 5-минутных свечах.
    DEFAULT_SLIPPAGE_BPS = 5.0

    maker_fee_rate: float = DEFAULT_MAKER_FEE_RATE
    taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    slippage_enabled: bool = True

    # Тип ликвидности по умолчанию — тейкер с обеих сторон: вход по
    # рынку и выход по стопу/цели исполняются агрессивно.
    entry_liquidity: str = TAKER
    exit_liquidity: str = TAKER

    @classmethod
    def from_env(cls) -> "TradingCostConfig":
        return cls(
            maker_fee_rate=_float_from_env(
                "PAPER_MAKER_FEE_RATE",
                cls.DEFAULT_MAKER_FEE_RATE,
            ),
            taker_fee_rate=_float_from_env(
                "PAPER_TAKER_FEE_RATE",
                cls.DEFAULT_TAKER_FEE_RATE,
            ),
            slippage_bps=_float_from_env(
                "PAPER_SLIPPAGE_BPS",
                cls.DEFAULT_SLIPPAGE_BPS,
            ),
            slippage_enabled=_flag_from_env(
                "PAPER_SLIPPAGE_ENABLED",
                True,
            ),
            entry_liquidity=_liquidity_from_env(
                "PAPER_ENTRY_LIQUIDITY",
                TAKER,
            ),
            exit_liquidity=_liquidity_from_env(
                "PAPER_EXIT_LIQUIDITY",
                TAKER,
            ),
        )

    @classmethod
    def zero_cost(cls) -> "TradingCostConfig":
        """
        Модель без издержек — только для тестов чистой арифметики.

        Отключение обязано быть ЯВНЫМ: получить нулевые издержки
        случайно, забыв настроить окружение, невозможно.
        """
        return cls(
            maker_fee_rate=0.0,
            taker_fee_rate=0.0,
            slippage_bps=0.0,
            slippage_enabled=False,
        )

    @property
    def slippage_rate(self) -> float:
        if not self.slippage_enabled:
            return 0.0

        return self.slippage_bps * BASIS_POINT

    def fee_rate(self, liquidity: str) -> float:
        return (
            self.maker_fee_rate
            if liquidity == MAKER
            else self.taker_fee_rate
        )

    def snapshot(self) -> dict[str, Any]:
        """Снимок конфигурации для записи в журнал сделки."""
        return {
            "fee_model_version": self.VERSION,
            "maker_fee_rate": self.maker_fee_rate,
            "taker_fee_rate": self.taker_fee_rate,
            "slippage_bps": self.slippage_bps,
            "slippage_enabled": self.slippage_enabled,
            "slippage_rate": self.slippage_rate,
            "entry_liquidity": self.entry_liquidity,
            "exit_liquidity": self.exit_liquidity,
        }


def apply_slippage(
    price: float,
    *,
    side: str,
    is_entry: bool,
    config: TradingCostConfig,
) -> float:
    """
    Сдвигает цену исполнения ПРОТИВ сделки.

    Для лонга: вход дороже, выход дешевле. Для шорта — наоборот.
    Проскальзывание никогда не улучшает результат: односторонняя
    модель занижает результат, что для paper-оценки безопаснее
    симметричной.
    """
    rate = config.slippage_rate

    if rate == 0.0:
        return float(price)

    adverse_up = (normalise_side(side) == LONG) == is_entry

    factor = (1.0 + rate) if adverse_up else (1.0 - rate)

    return float(price) * factor


def compute_trade_costs(
    *,
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: str = LONG,
    config: TradingCostConfig | None = None,
) -> dict[str, Any]:
    """
    Полный расчёт результата сделки с издержками.

    gross_pnl считается по СЫРЫМ ценам — это идеальный результат без
    трения. net_pnl считается по эффективным ценам и дополнительно
    уменьшается на комиссии. Разница между ними и есть цена исполнения.
    """
    config = config or TradingCostConfig.from_env()
    side = normalise_side(side)

    entry_raw = float(entry_price)
    exit_raw = float(exit_price)
    size = float(quantity)

    entry_effective = apply_slippage(
        entry_raw,
        side=side,
        is_entry=True,
        config=config,
    )

    exit_effective = apply_slippage(
        exit_raw,
        side=side,
        is_entry=False,
        config=config,
    )

    direction = 1.0 if side == LONG else -1.0

    gross_pnl = (exit_raw - entry_raw) * size * direction

    # Стоимость проскальзывания — всегда неотрицательная величина,
    # на которую результат ухудшился относительно сырых цен.
    slippage_cost = (
        abs(entry_effective - entry_raw)
        + abs(exit_effective - exit_raw)
    ) * size

    entry_fee = (
        entry_effective
        * size
        * config.fee_rate(config.entry_liquidity)
    )

    exit_fee = (
        exit_effective
        * size
        * config.fee_rate(config.exit_liquidity)
    )

    total_fees = entry_fee + exit_fee

    net_pnl = (
        (exit_effective - entry_effective) * size * direction
        - total_fees
    )

    return {
        "entry_price_raw": round(entry_raw, 8),
        "entry_price_effective": round(entry_effective, 8),
        "exit_price_raw": round(exit_raw, 8),
        "exit_price_effective": round(exit_effective, 8),
        "quantity": round(size, 8),
        "side": side,
        "gross_pnl": round(gross_pnl, 8),
        "entry_fee": round(entry_fee, 8),
        "exit_fee": round(exit_fee, 8),
        "total_fees": round(total_fees, 8),
        "slippage_cost": round(slippage_cost, 8),
        "net_pnl": round(net_pnl, 8),
        "fee_model_version": config.VERSION,
        "cost_config": config.snapshot(),
    }
