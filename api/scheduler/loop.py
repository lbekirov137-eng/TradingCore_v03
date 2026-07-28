"""
Управляемый цикл paper-исполнения.

Каждая итерация: рыночные данные -> фильтры -> стратегия -> решение ->
paper-исполнение (Workflow.run, уже покрыт тестами) -> проверка выхода
(ExitMonitor) -> reconciliation отложенных ордеров -> heartbeat -> алерты.

НЕТ LOOK-AHEAD: цикл не добавляет никакой новой точки утечки данных —
он лишь решает, КОГДА вызвать уже существующий Workflow.run(), и делает
это по завершении свечи (не раньше). Сама защита от использования
незакрытой свечи находится ниже по стеку, в market_hub.py
(drop_unclosed_candle), и не дублируется здесь.

Экземпляр цикла не ловит сигналы ОС сам по себе — это осознанно, чтобы
не мешать процессу, в котором цикл встроен (например, тестам или
uvicorn). Обработка SIGINT/SIGTERM для автономного запуска находится в
scripts/run_paper_loop.py.
"""

import time

from api.contracts.context import LiveContext
from api.workflow.workflow import Workflow
from api.execution.exit_monitor import ExitMonitor
from api.observability.states import health, logger, SystemState
from api.observability.telegram_mock import telegram
from api.market_data.candle_utils import INTERVAL_MS


class SchedulerLoop:

    def __init__(self, exchange: str, symbol: str, interval: str, limit: int,
                 adapter, trade_engine, reconciler=None, poll_buffer_seconds: float = 3.0,
                 replay_mode: bool = False):

        self.exchange = exchange
        self.symbol = symbol
        self.interval = interval
        self.limit = limit
        self.adapter = adapter
        self.trade_engine = trade_engine
        self.exit_monitor = ExitMonitor(adapter=adapter, trade_engine=trade_engine)
        self.reconciler = reconciler
        self.poll_buffer_seconds = poll_buffer_seconds

        # ВАЖНО: replay_mode должен оставаться False в любом реальном
        # (paper-forward/demo) запуске — иначе проверка устаревания данных
        # перестанет защищать от залипшего фида. True предназначен только
        # для тестов/детерминированного воспроизведения истории.
        self.replay_mode = replay_mode

        self._stop_requested = False
        self._iterations = 0

    def stop(self):
        """Запрашивает остановку. Текущая итерация (если идёт) завершается штатно."""
        self._stop_requested = True

    def is_stopping(self) -> bool:
        return self._stop_requested

    def seconds_until_next_close(self, now: float = None) -> float:
        """
        Сколько секунд подождать до момента, когда следующая свеча биржи
        гарантированно закроется (плюс буфер на публикацию биржей).
        """
        now = now if now is not None else time.time()
        interval_seconds = INTERVAL_MS[self.interval] / 1000
        elapsed = now % interval_seconds
        remaining = interval_seconds - elapsed
        return remaining + self.poll_buffer_seconds

    def run_once(self) -> dict:
        """
        Один тик цикла. Безопасен к любой ошибке внутри — не бросает наружу.

        Правило: если рантайм-проверка конфигурации (config/startup_safety.py)
        обнаруживает попытку live-режима или нераспознанную конфигурацию —
        цикл немедленно останавливается как FAILED_SAFELY, а не продолжает
        тикать. Это отдельная, повторная проверка поверх той, что уже
        выполняется один раз при импорте api/server.py.
        """

        from config.startup_safety import runtime_safety_check

        safety = runtime_safety_check()
        if not safety["safe"]:
            message = f"Небезопасная конфигурация обнаружена в рантайме: {safety['reason']}"
            logger.log(SystemState.FAILED_SAFELY, message)
            health.record_error(message)
            self.stop()  # цикл останавливается, процесс/HTTP-сервер продолжает работать
            return {"decision": None, "execution": None, "exit": None,
                     "reconciliation": None, "error": message, "failed_safely": True}

        try:
            context = LiveContext(
                exchange=self.exchange, symbol=self.symbol,
                interval=self.interval, limit=self.limit,
                replay_mode=self.replay_mode,
            )

            result = Workflow.run(context)

            decision = result["decision"]
            execution = result["execution"]

            health.heartbeat()

            market = context.market
            if market is not None and len(getattr(market, "timestamps", [])) > 0:
                health.record_market_data(market.timestamps[-1])

            self._log_decision(decision, execution)

            exit_result = self._check_exit(market)

            reconcile_results = self._reconcile()

            return {
                "decision": decision,
                "execution": execution,
                "exit": exit_result,
                "reconciliation": reconcile_results,
            }

        except Exception as error:
            # Граница безопасности цикла: ЛЮБАЯ необработанная ошибка тика
            # приводит к безопасной остановке этой итерации, а не к падению
            # процесса. Цикл продолжит работу со следующей итерации.
            message = f"{type(error).__name__}: {error}"
            logger.log(SystemState.FAILED_SAFELY, f"Ошибка тика цикла: {message}")
            health.record_error(message)
            self._alert(f"Ошибка тика планировщика: {message}", level="ERROR")
            return {"decision": None, "execution": None, "exit": None,
                     "reconciliation": None, "error": message}

    def _check_exit(self, market):
        if market is None or len(getattr(market, "timestamps", [])) == 0:
            return None

        candle = {
            "symbol": self.symbol,
            "high": market.highs[-1],
            "low": market.lows[-1],
            "close": market.closes[-1],
        }

        result = self.exit_monitor.check(candle)

        if result.get("action") in ("CLOSED", "PARTIALLY_CLOSED"):
            self._alert(
                f"Позиция {result['action']}: {result.get('exit_reason')} @ {result.get('exit_price')}"
            )
            logger.log(SystemState.CLOSED, "Позиция закрыта exit-монитором.", **result)
        elif result.get("action") == "RECONCILE_FAILED":
            logger.log(SystemState.RECONCILING, "Сверка позиции не удалась.", **result)

        return result

    def _reconcile(self):
        if self.reconciler is None:
            return None

        results = self.reconciler.reconcile_all_pending()

        if results:
            health.record_reconciliation()
            logger.log(SystemState.RECONCILING, "Отложенные ордера сверены.", count=len(results))

        return results

    def _log_decision(self, decision, execution):

        if execution.get("status") == "OPENED":
            state = SystemState.OPENED
            self._alert(
                f"Открыта paper-позиция: {decision.get('symbol')} {decision.get('direction')} "
                f"@ {decision.get('trade_plan', {}).get('entry')}"
            )
        elif execution.get("status") == "FAILED_SAFELY":
            state = SystemState.FAILED_SAFELY
        elif execution.get("status") == "ORDER_PENDING":
            state = SystemState.ORDER_PENDING
        elif execution.get("status") == "PARTIALLY_FILLED":
            state = SystemState.PARTIALLY_FILLED
        else:
            state = SystemState.NO_TRADE

        logger.log(state, decision.get("reason", ""), decision=decision, execution=execution)

    def _alert(self, text: str, level: str = "INFO"):
        telegram.send(text, level=level)

    def run_forever(self, max_iterations: int = None, sleep_fn=time.sleep):
        """
        Основной цикл. max_iterations используется тестами/backtest-режимом,
        чтобы не блокироваться реальным ожиданием — в проде оставляется None.
        Останавливается по stop() (graceful shutdown) или по max_iterations.
        """

        logger.log(SystemState.STARTING, "Цикл paper-исполнения запускается.",
                   exchange=self.exchange, symbol=self.symbol, interval=self.interval)

        self._stop_requested = False
        self._iterations = 0

        while not self._stop_requested:

            if max_iterations is not None and self._iterations >= max_iterations:
                break

            logger.log(SystemState.HEALTHY, "Тик цикла.", iteration=self._iterations)

            self.run_once()

            self._iterations += 1

            if self._stop_requested:
                break

            if max_iterations is not None:
                continue  # detministic test/backtest mode — no real sleeping

            sleep_fn(self.seconds_until_next_close())

        logger.log(SystemState.STOPPED, "Цикл paper-исполнения остановлен.",
                   iterations=self._iterations)

        return {"iterations": self._iterations}
