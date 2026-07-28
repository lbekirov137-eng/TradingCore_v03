"""
Автономный запуск цикла paper-исполнения с корректной обработкой
SIGINT/SIGTERM (graceful shutdown).

Реальные ордера не отправляются ни при каких условиях — используется
только PaperBroker.

Пример:
    python scripts/run_paper_loop.py --symbol BTCUSDT --interval 5m
"""

import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.scheduler.loop import SchedulerLoop
from api.trade_engine import trade_engine as te
from api.execution.order_reconciler import OrderReconciler
from api.observability.states import logger, SystemState
from config.settings import DEFAULT_SYMBOL, DEFAULT_INTERVAL, DEFAULT_CANDLE_LIMIT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--limit", type=int, default=DEFAULT_CANDLE_LIMIT)
    args = parser.parse_args()

    reconciler = OrderReconciler(te.broker, te.idempotency_store)

    loop = SchedulerLoop(
        exchange=args.exchange, symbol=args.symbol, interval=args.interval,
        limit=args.limit, adapter=te.broker, trade_engine=te.TradeEngine,
        reconciler=reconciler,
    )

    def handle_signal(signum, frame):
        logger.log(SystemState.STOPPED, f"Получен сигнал {signum} — запрошена graceful-остановка.")
        loop.stop()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    print(f"Paper loop starting: {args.exchange} {args.symbol} {args.interval}. Ctrl+C to stop.")

    result = loop.run_forever()

    print(f"Stopped after {result['iterations']} iterations.")


if __name__ == "__main__":
    main()
