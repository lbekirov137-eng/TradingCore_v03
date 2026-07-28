from api.scheduler.scheduler import Scheduler
from api.trade_engine.trade_engine import TradeEngine


class Workflow:

    @staticmethod
    def run(context):

        """
        Единая точка запуска всей торговой системы (paper/demo).

        Любой запуск начинается отсюда:
        - REST API
        - n8n
        - Telegram
        - Scheduler
        - тестирование
        - облачный сервер

        Реальные ордера здесь никогда не создаются — TradeEngine.execute
        работает только в paper-режиме (симуляция в памяти процесса).
        """

        decision = Scheduler.tick(context)

        execution = TradeEngine.execute(decision)

        return {
            "decision": decision,
            "execution": execution,
        }