"""
Хранилище состояний ордеров с восстановлением после рестарта.

По умолчанию хранит записи в памяти процесса + пишет их на диск (JSON,
по одному файлу на запись) в директорию `state/orders/` — так после
рестарта процесс может восстановить, какие ордера были в неопределённом
состоянии, и провести reconciliation ПЕРЕД тем, как решать, разрешать ли
новые сделки (см. api/execution/order_reconciler.py и Phase 1.6 kill switch).

Файловое хранилище — намеренно простое (без внешней БД), т.к. это
paper/demo MVP: цель — пережить рестарт процесса, а не быть
production-grade хранилищем для реальных денег.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from api.execution.order_state import OrderStatus


DEFAULT_STATE_DIR = os.path.join("state", "orders")


@dataclass
class OrderRecord:

    client_order_id: str
    decision: dict
    status: str = OrderStatus.NEW.value
    exchange_order_id: Optional[str] = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_error: Optional[str] = None


class IdempotencyStore:

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        self._records: dict[str, OrderRecord] = {}
        os.makedirs(self.state_dir, exist_ok=True)
        self._load_from_disk()

    def _path_for(self, client_order_id: str) -> str:
        return os.path.join(self.state_dir, f"{client_order_id}.json")

    def _load_from_disk(self):
        if not os.path.isdir(self.state_dir):
            return

        for filename in os.listdir(self.state_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.state_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._records[data["client_order_id"]] = OrderRecord(**data)
            except (json.JSONDecodeError, OSError, TypeError, KeyError):
                # Повреждённый файл состояния не должен ронять процесс —
                # он просто не восстанавливается (см. Red Team сценарий
                # "журнал/checkpoint повреждён" — безопасный отказ, не крэш).
                continue

    def _persist(self, record: OrderRecord):
        path = self._path_for(record.client_order_id)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(record), f)
        os.replace(tmp_path, path)  # atomic on POSIX and Windows

    def get(self, client_order_id: str) -> Optional[OrderRecord]:
        return self._records.get(client_order_id)

    def get_or_create(self, client_order_id: str, decision: dict) -> OrderRecord:
        """
        Идемпотентность на нашей стороне: если запись уже существует —
        возвращает её БЕЗ создания нового ордера (тот же decision мог
        прийти повторно из-за ретрая после таймаута).
        """
        existing = self._records.get(client_order_id)
        if existing is not None:
            return existing

        record = OrderRecord(client_order_id=client_order_id, decision=decision)
        self._records[client_order_id] = record
        self._persist(record)
        return record

    def update_status(self, client_order_id: str, status: OrderStatus,
                       exchange_order_id: Optional[str] = None,
                       last_error: Optional[str] = None) -> OrderRecord:

        record = self._records.get(client_order_id)
        if record is None:
            raise KeyError(f"Unknown client_order_id: {client_order_id}")

        record.status = status.value if isinstance(status, OrderStatus) else status
        if exchange_order_id is not None:
            record.exchange_order_id = exchange_order_id
        if last_error is not None:
            record.last_error = last_error
        record.updated_at = time.time()

        self._persist(record)
        return record

    def increment_attempts(self, client_order_id: str) -> OrderRecord:
        record = self._records.get(client_order_id)
        if record is None:
            raise KeyError(f"Unknown client_order_id: {client_order_id}")
        record.attempts += 1
        record.updated_at = time.time()
        self._persist(record)
        return record

    def all_records(self) -> list:
        return list(self._records.values())

    def pending_or_unknown(self) -> list:
        from api.execution.order_state import NON_RETRYABLE_WITHOUT_RECONCILIATION
        pending_values = {s.value for s in NON_RETRYABLE_WITHOUT_RECONCILIATION}
        return [r for r in self._records.values() if r.status in pending_values]

    def reset(self):
        """Только для тестов: очищает память И диск."""
        for record in list(self._records.values()):
            path = self._path_for(record.client_order_id)
            if os.path.exists(path):
                os.remove(path)
        self._records.clear()
