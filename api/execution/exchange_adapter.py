"""
Единый интерфейс исполнения — и paper-брокер (Phase 5), и Bybit Demo
адаптер (Phase 6) реализуют один и тот же контракт. Это то, что
позволяет reconciliation-логике (order_reconciler.py) и exit-монитору
работать одинаково независимо от того, где на самом деле "живёт" ордер:
в памяти paper-брокера или на демо-счёте Bybit.

Принцип "exchange-state-is-source-of-truth": локальное состояние —
это гипотеза, которая обязана сверяться с get_order/get_position
биржи, а не наоборот.
"""

from abc import ABC, abstractmethod


class ExchangeAdapter(ABC):

    @abstractmethod
    def place_order(self, client_order_id: str, order: dict) -> dict:
        """Отправляет ордер. Должен быть идемпотентным по client_order_id —
        повторный вызов с тем же client_order_id не должен создавать второй ордер."""

    @abstractmethod
    def amend_order(self, client_order_id: str, changes: dict) -> dict:
        ...

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> dict:
        ...

    @abstractmethod
    def get_order(self, client_order_id: str) -> dict:
        """Возвращает состояние ордера по client_order_id, включая
        'not_found', если биржа никогда не получала такой ордер."""

    @abstractmethod
    def get_open_orders(self, symbol: str = None) -> list:
        ...

    @abstractmethod
    def get_executions(self, client_order_id: str = None) -> list:
        ...

    @abstractmethod
    def get_position(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def get_balance(self) -> dict:
        ...
