import math


def _is_bad_number(value) -> bool:
    """
    True, если значение не является пригодным для расчёта числом.

    КРИТИЧНО: проверка вида `atr <= 0` НЕ отсеивает NaN и Infinity —
    в IEEE-754 `float('nan') <= 0` равно False, поэтому NaN проходил
    такую проверку насквозь и приводил к "allowed": True с NaN размером
    позиции. Это подтверждённая критическая находка аудита; здесь она
    закрыта явной проверкой типа/NaN/Infinity до любых вычислений.
    """
    if value is None or isinstance(value, bool):
        return True
    if not isinstance(value, (int, float)):
        return True
    if math.isnan(value) or math.isinf(value):
        return True
    return False


class RiskEngine:

    @staticmethod
    def calculate(
        balance: float,
        risk_percent: float,
        price: float,
        atr: float,
    ):
        for name, value in (
            ("balance", balance),
            ("risk_percent", risk_percent),
            ("price", price),
            ("atr", atr),
        ):
            if _is_bad_number(value):
                return {
                    "allowed": False,
                    "reason": f"Некорректное значение параметра {name} (None/NaN/Infinity/не число)",
                }

        if balance <= 0:
            return {"allowed": False, "reason": "Balance must be positive"}

        if risk_percent <= 0:
            return {"allowed": False, "reason": "Risk percent must be positive"}

        if price <= 0:
            return {"allowed": False, "reason": "Price must be positive"}

        if atr <= 0:
            return {
                "allowed": False,
                "reason": "ATR is zero",
            }

        risk_amount = balance * (risk_percent / 100)
        stop_distance = atr
        position_size = risk_amount / stop_distance

        if position_size <= 0 or _is_bad_number(position_size):
            return {"allowed": False, "reason": "Calculated position size is not positive"}

        return {
            "allowed": True,
            "risk_amount": round(risk_amount, 2),
            "position_size": round(position_size, 6),
            "stop_distance": round(stop_distance, 2),
        }

    @staticmethod
    def calculate_by_stop(
        balance: float,
        risk_percent: float,
        entry: float,
        stop: float,
    ):
        for name, value in (
            ("balance", balance),
            ("risk_percent", risk_percent),
            ("entry", entry),
            ("stop", stop),
        ):
            if _is_bad_number(value):
                return {
                    "allowed": False,
                    "reason": f"Некорректное значение параметра {name} (None/NaN/Infinity/не число)",
                }

        if balance <= 0:
            return {"allowed": False, "reason": "Balance must be positive"}

        if risk_percent <= 0:
            return {"allowed": False, "reason": "Risk percent must be positive"}

        if entry <= 0 or stop <= 0:
            return {"allowed": False, "reason": "Entry and stop must be positive"}

        stop_distance = abs(float(entry) - float(stop))

        if stop_distance <= 0:
            return {
                "allowed": False,
                "reason": "Stop distance is zero",
            }

        risk_amount = balance * (risk_percent / 100)
        position_size = risk_amount / stop_distance

        if position_size <= 0 or _is_bad_number(position_size):
            return {"allowed": False, "reason": "Calculated position size is not positive"}

        return {
            "allowed": True,
            "risk_amount": round(risk_amount, 2),
            "position_size": round(position_size, 6),
            "stop_distance": round(stop_distance, 2),
        }
