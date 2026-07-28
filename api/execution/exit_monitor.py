"""
Монитор выхода из позиции.

Закрывает открытую paper-позицию при срабатывании стоп-лосса,
тейк-профита, инвалидации сценария или устаревания позиции. Это
закрывает крупнейший пробел исходного аудита (F17: "открытая позиция
никогда не закрывалась сама").

КОНСЕРВАТИВНАЯ МОДЕЛЬ ОДНОЙ СВЕЧИ (важно):
Если внутри одной свечи диапазон [low, high] покрывает И стоп, И
тейк-профит, мы НЕ выбираем прибыльный исход. Порядок событий внутри
свечи неизвестен из OHLC-данных, поэтому предполагается ХУДШИЙ исход —
срабатывание стопа. Это прямо соответствует требованию ТЗ: "не выбирай
автоматически прибыльный вариант". Разрешение этой неоднозначности
точнее требует тиковых данных, которых у системы нет.

Источник истины — биржа/брокер: перед решением о закрытии монитор
сверяет локальное представление о позиции с get_position() адаптера.
"""

import time

from api.position_manager.position_manager import PositionManager


class ExitReason:
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT_1 = "TAKE_PROFIT_1"
    TAKE_PROFIT_2 = "TAKE_PROFIT_2"
    INVALIDATION = "INVALIDATION"
    STALE_POSITION = "STALE_POSITION"
    KILL_SWITCH = "KILL_SWITCH"


class ExitMonitor:

    def __init__(self, adapter, trade_engine, max_position_age_seconds: float = 24 * 3600):
        self.adapter = adapter
        self.trade_engine = trade_engine
        self.max_position_age_seconds = max_position_age_seconds

    def check(self, candle: dict, now: float = None, invalidated: bool = False) -> dict:
        """
        candle = {"symbol", "high", "low", "close"}

        Возвращает описание принятого действия. Если позиции нет —
        безопасно возвращает {"action": "NO_POSITION"} без исключений.
        """

        now = now if now is not None else time.time()

        if not PositionManager.has_open_position():
            return {"action": "NO_POSITION"}

        position = PositionManager.current_position()

        symbol = position.get("symbol")

        if candle.get("symbol") != symbol:
            return {"action": "SYMBOL_MISMATCH", "expected": symbol, "received": candle.get("symbol")}

        # Источник истины — состояние у брокера/биржи, а не наша память.
        reconciled = self._reconcile_position(symbol, position)
        if reconciled is not None:
            return reconciled

        high = candle.get("high")
        low = candle.get("low")
        close = candle.get("close")

        if high is None or low is None or high != high or low != low:
            return {"action": "INVALID_CANDLE", "reason": "Свеча не содержит корректных high/low."}

        stop = position.get("stop")
        take_profit = position.get("take_profit") or {}
        tp1 = take_profit.get("tp1")

        stop_hit = stop is not None and low <= stop <= high
        tp_hit = tp1 is not None and low <= tp1 <= high

        if stop_hit and tp_hit:
            # Консервативно: порядок внутри свечи неизвестен -> считаем,
            # что первым сработал стоп. Прибыльный исход НЕ выбирается.
            return self._close(position, ExitReason.STOP_LOSS, stop,
                                note="Стоп и тейк в одной свече — выбран консервативный (худший) исход.")

        if stop_hit:
            return self._close(position, ExitReason.STOP_LOSS, stop)

        if tp_hit:
            return self._close(position, ExitReason.TAKE_PROFIT_1, tp1)

        if invalidated:
            return self._close(position, ExitReason.INVALIDATION, close)

        opened_at = position.get("opened_at")
        if opened_at is not None and (now - opened_at) > self.max_position_age_seconds:
            return self._close(position, ExitReason.STALE_POSITION, close)

        return {"action": "HOLD", "symbol": symbol}

    def _reconcile_position(self, symbol, position):
        """
        Сверяет локальную позицию с состоянием у брокера. Если брокер
        сообщает, что позиции нет (например, она была закрыта вне нашего
        процесса или потеряна), локальное состояние приводится в
        соответствие — без выдумывания несуществующей позиции.
        """

        try:
            exchange_position = self.adapter.get_position(symbol)
        except Exception as error:
            # Сбой сверки не должен приводить к слепому закрытию.
            return {"action": "RECONCILE_FAILED", "reason": f"{type(error).__name__}: {error}"}

        exchange_qty = exchange_position.get("qty", 0.0)

        if exchange_qty <= 0:
            PositionManager.close_position("reconciled_flat_on_exchange")
            return {
                "action": "RECONCILED_FLAT",
                "reason": "Брокер сообщает об отсутствии позиции — локальное состояние синхронизировано.",
            }

        return None

    def _close(self, position, reason: str, exit_price: float, note: str = None):

        result = self.trade_engine.close(reason=reason, exit_price=exit_price)

        # Не рапортуем CLOSED, если исполнение фактически не завершилось:
        # ордер мог остаться частично исполненным или в неопределённом
        # состоянии. Иначе монитор "потеряет" позицию, которая на самом
        # деле всё ещё открыта.
        execution_status = (result or {}).get("status")

        if execution_status == "CLOSED":
            action = "CLOSED"
        elif execution_status == "PARTIALLY_FILLED":
            action = "PARTIALLY_CLOSED"
        else:
            action = "CLOSE_FAILED"

        payload = {
            "action": action,
            "exit_reason": reason,
            "exit_price": exit_price,
            "symbol": position.get("symbol"),
            "execution": result,
        }

        if note:
            payload["note"] = note

        return payload
