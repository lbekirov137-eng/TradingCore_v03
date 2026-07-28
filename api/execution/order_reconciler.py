"""
Reconciliation: приводит локальное состояние ордера в соответствие с
фактическим состоянием на бирже (ExchangeAdapter.get_order). Локальное
состояние — это гипотеза, биржа — источник истины.

Ключевое правило (из ТЗ): "Never resend an order merely because the
first request timed out." Таймаут/сетевая ошибка при отправке переводит
ордер в SUBMITTED (не FAILED, не повторяется автоматически). Следующий
шаг всегда — спросить биржу через get_order(client_order_id):
  - биржа знает об ордере (ACKNOWLEDGED/FILLED/PARTIALLY_FILLED/...)
      -> локальное состояние обновляется, повторная отправка НЕ нужна;
  - биржа не знает об ордере (not_found) и прошло достаточно времени
      -> ордер безопасно считается никогда не полученным биржей,
         разрешается повторная попытка (с НОВЫМ client_order_id,
         генерируемым вызывающим кодом на основе актуального решения);
  - неоднозначный ответ/ошибка при самом запросе reconciliation
      -> остаётся UNKNOWN, ничего не меняется, повтор запрещён.
"""

import time

from api.execution.order_state import OrderStatus
from api.execution.idempotency_store import IdempotencyStore


class OrderReconciler:

    def __init__(self, adapter, store: IdempotencyStore, unknown_grace_period_seconds: float = 30.0):
        self.adapter = adapter
        self.store = store
        self.unknown_grace_period_seconds = unknown_grace_period_seconds

    def reconcile_one(self, client_order_id: str) -> dict:

        record = self.store.get(client_order_id)

        if record is None:
            return {"client_order_id": client_order_id, "outcome": "no_local_record"}

        try:
            exchange_state = self.adapter.get_order(client_order_id)
        except Exception as error:
            # Сам запрос reconciliation не удался — остаёмся UNKNOWN,
            # ничего не додумываем, ничего не отправляем повторно.
            self.store.update_status(
                client_order_id, OrderStatus.UNKNOWN,
                last_error=f"reconciliation query failed: {type(error).__name__}: {error}",
            )
            return {"client_order_id": client_order_id, "outcome": "reconciliation_query_failed"}

        found = exchange_state.get("found", False)

        if not found:
            age_seconds = time.time() - record.created_at

            if age_seconds < self.unknown_grace_period_seconds:
                # Слишком рано делать вывод — биржа могла ещё не обработать.
                return {"client_order_id": client_order_id, "outcome": "too_early_to_conclude"}

            # Прошло достаточно времени, и биржа подтверждает, что НИКОГДА
            # не получала этот ордер -> безопасно считать его несостоявшимся.
            self.store.update_status(client_order_id, OrderStatus.CANCELLED,
                                      last_error="Биржа не подтвердила получение ордера — считается неотправленным.")
            return {"client_order_id": client_order_id, "outcome": "confirmed_never_received", "retry_allowed": True}

        exchange_status = exchange_state.get("status")
        exchange_order_id = exchange_state.get("exchange_order_id")

        self.store.update_status(client_order_id, exchange_status, exchange_order_id=exchange_order_id)

        return {
            "client_order_id": client_order_id,
            "outcome": "reconciled",
            "status": exchange_status,
        }

    def reconcile_all_pending(self) -> list:
        results = []
        for record in self.store.pending_or_unknown():
            results.append(self.reconcile_one(record.client_order_id))
        return results
