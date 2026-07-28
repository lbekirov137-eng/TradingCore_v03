"""
Фоновый cloud paper monitor: непрерывно тикает SchedulerLoop в отдельном
потоке, пока живёт процесс FastAPI/uvicorn. Без этого приложение отвечало
бы на HTTP, но не вело бы никакого автономного paper-forward наблюдения —
только вручную дёргаемый /paper/tick.

Использует ИСКЛЮЧИТЕЛЬНО PaperBroker (Execution Simulator) — реальный
Exchange Router (реальные ключи/ордера) здесь не участвует и не может
участвовать, так как в кодовой базе нет ни одного вызова, создающего
живой ордер на бирже.
"""

import threading

from api.scheduler.loop import SchedulerLoop
from api.trade_engine import trade_engine as te
from api.execution.order_reconciler import OrderReconciler
from api.observability.states import logger, SystemState
from config.settings import DEFAULT_SYMBOL, DEFAULT_INTERVAL, DEFAULT_CANDLE_LIMIT


class CloudMonitor:

    def __init__(self, exchange: str = "binance", symbol: str = DEFAULT_SYMBOL,
                 interval: str = DEFAULT_INTERVAL, limit: int = DEFAULT_CANDLE_LIMIT):

        reconciler = OrderReconciler(te.broker, te.idempotency_store)

        self.loop = SchedulerLoop(
            exchange=exchange, symbol=symbol, interval=interval, limit=limit,
            adapter=te.broker, trade_engine=te.TradeEngine, reconciler=reconciler,
            replay_mode=False,  # ВСЕГДА False в облачном запуске — реальные часы, реальная проверка stale-данных
        )

        self._thread: threading.Thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return  # уже запущен -- идемпотентно, без второго потока

        logger.log(SystemState.STARTING, "Cloud paper monitor: запуск фонового потока.")

        self._thread = threading.Thread(
            target=self.loop.run_forever,
            kwargs={"max_iterations": None},
            daemon=True,   # поток не блокирует штатное завершение процесса
            name="cloud-paper-monitor",
        )
        self._thread.start()

    def stop(self):
        self.loop.stop()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# Единственный экземпляр монитора процесса.
monitor = CloudMonitor()
