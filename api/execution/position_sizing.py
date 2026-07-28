"""
Полный расчёт размера позиции для spot long/short без плеча.

Формула:
  1. risk_amount = balance * (risk_percent / 100)
  2. slippage_amount = entry * (slippage_bps / 10_000)   -- на каждую сторону сделки
  3. effective_stop_distance = |entry - stop| + 2 * slippage_amount
     (проскальзывание закладывается и на вход, и на выход)
  4. fee_amount_per_unit = fee_rate * (entry + stop)      -- комиссия на вход + выход
  5. quantity = risk_amount / (effective_stop_distance + fee_amount_per_unit)
  6. quantity округляется ВНИЗ до ближайшего шага lot_size (никогда вверх —
     округление вверх увеличило бы риск сверх заданного)
  7. price округляется до ближайшего tick_size
  8. notional = quantity * entry; отклоняется, если notional < min_notional
     (слишком маленький ордер для биржи) или notional > available_balance
     (означало бы плечо, что запрещено)

Все входные данные валидируются на NaN/Infinity/None/отрицательные значения
до вычислений — при любой аномалии результат безопасен (NOT allowed),
никогда не бросает исключение и никогда не даёт NaN/inf/0 "одобренный" размер.
"""

import math
from dataclasses import dataclass


def _is_bad_number(value) -> bool:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    if math.isnan(value) or math.isinf(value):
        return True
    return False


@dataclass
class PositionSizeResult:
    allowed: bool
    reason: str = ""
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    fee_amount: float = 0.0
    slippage_amount: float = 0.0
    effective_stop_distance: float = 0.0


class PositionSizer:

    @staticmethod
    def calculate(
        balance: float,
        available_balance: float,
        risk_percent: float,
        entry: float,
        stop: float,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        tick_size: float = 0.01,
        lot_size: float = 0.000001,
        min_notional: float = 5.0,
        max_position_percent_of_balance: float = 100.0,
    ) -> PositionSizeResult:

        numeric_inputs = {
            "balance": balance,
            "available_balance": available_balance,
            "risk_percent": risk_percent,
            "entry": entry,
            "stop": stop,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "tick_size": tick_size,
            "lot_size": lot_size,
            "min_notional": min_notional,
            "max_position_percent_of_balance": max_position_percent_of_balance,
        }

        for name, value in numeric_inputs.items():
            if _is_bad_number(value):
                return PositionSizeResult(allowed=False, reason=f"Некорректное значение параметра {name}.")

        if balance <= 0:
            return PositionSizeResult(allowed=False, reason="Баланс должен быть положительным.")

        if available_balance <= 0:
            return PositionSizeResult(allowed=False, reason="Доступный баланс должен быть положительным.")

        if available_balance > balance:
            return PositionSizeResult(allowed=False, reason="available_balance не может превышать balance.")

        if risk_percent <= 0:
            return PositionSizeResult(allowed=False, reason="risk_percent должен быть положительным.")

        if entry <= 0 or stop <= 0:
            return PositionSizeResult(allowed=False, reason="entry и stop должны быть положительными.")

        if entry == stop:
            return PositionSizeResult(allowed=False, reason="entry не может быть равен stop.")

        if fee_rate < 0 or slippage_bps < 0:
            return PositionSizeResult(allowed=False, reason="fee_rate и slippage_bps не могут быть отрицательными.")

        if tick_size <= 0 or lot_size <= 0:
            return PositionSizeResult(allowed=False, reason="tick_size и lot_size должны быть положительными.")

        if min_notional < 0:
            return PositionSizeResult(allowed=False, reason="min_notional не может быть отрицательным.")

        if max_position_percent_of_balance <= 0:
            return PositionSizeResult(allowed=False, reason="max_position_percent_of_balance должен быть положительным.")

        entry_rounded = PositionSizer.round_to_tick(entry, tick_size)
        stop_rounded = PositionSizer.round_to_tick(stop, tick_size)

        risk_amount = balance * (risk_percent / 100)

        slippage_amount = entry_rounded * (slippage_bps / 10_000)

        raw_stop_distance = abs(entry_rounded - stop_rounded)

        if raw_stop_distance <= 0:
            return PositionSizeResult(allowed=False, reason="Нулевое расстояние до стопа после округления до tick_size.")

        effective_stop_distance = raw_stop_distance + 2 * slippage_amount

        fee_amount_per_unit = fee_rate * (entry_rounded + stop_rounded)

        denominator = effective_stop_distance + fee_amount_per_unit

        if denominator <= 0:
            return PositionSizeResult(allowed=False, reason="Некорректный знаменатель при расчёте размера позиции.")

        raw_quantity = risk_amount / denominator

        quantity = PositionSizer.floor_to_lot(raw_quantity, lot_size)

        if quantity <= 0:
            return PositionSizeResult(
                allowed=False,
                reason="Рассчитанный размер позиции округляется до нуля при заданном lot_size.",
            )

        notional = quantity * entry_rounded

        if notional < min_notional:
            return PositionSizeResult(
                allowed=False,
                reason=f"Notional {notional:.2f} меньше минимального {min_notional:.2f}.",
                quantity=quantity, notional=notional, risk_amount=risk_amount,
            )

        max_notional = available_balance * (max_position_percent_of_balance / 100)

        if notional > max_notional:
            return PositionSizeResult(
                allowed=False,
                reason=(
                    f"Notional {notional:.2f} превышает лимит "
                    f"{max_notional:.2f} ({max_position_percent_of_balance}% от available_balance) "
                    f"— без плеча позиция не может быть открыта."
                ),
                quantity=quantity, notional=notional, risk_amount=risk_amount,
            )

        if notional > available_balance:
            return PositionSizeResult(
                allowed=False,
                reason="Notional превышает доступный баланс — потребовалось бы плечо, что запрещено.",
                quantity=quantity, notional=notional, risk_amount=risk_amount,
            )

        fee_amount = fee_amount_per_unit * quantity

        return PositionSizeResult(
            allowed=True,
            reason="",
            quantity=quantity,
            notional=round(notional, 8),
            risk_amount=round(risk_amount, 8),
            fee_amount=round(fee_amount, 8),
            slippage_amount=round(slippage_amount, 8),
            effective_stop_distance=round(effective_stop_distance, 8),
        )

    @staticmethod
    def round_to_tick(price: float, tick_size: float) -> float:
        return round(round(price / tick_size) * tick_size, 10)

    @staticmethod
    def floor_to_lot(quantity: float, lot_size: float) -> float:
        steps = math.floor(quantity / lot_size)
        return round(steps * lot_size, 10)
