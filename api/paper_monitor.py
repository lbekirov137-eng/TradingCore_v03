"""
Фоновый PAPER-монитор, живущий внутри процесса uvicorn.

Зачем именно так, а не отдельным процессом:
  - Railway healthcheck требует, чтобы порт слушал HTTP-сервер; отдельный
    процесс-цикл без сервера приводит к провалу healthcheck и рестарт-петле;
  - paper_watchdog.py поднимает цикл и Telegram-листенер как subprocess и
    жёстко падает, если листенер отсутствует или завершился, — в облаке это
    делает сервис нестабильным без всякой пользы для PAPER-контура;
  - uvicorn должен оставаться PID 1, чтобы получать SIGTERM напрямую
    (иначе контейнер убивается SIGKILL и обрывает запись журнала).

Инварианты безопасности:
  - монитор ВЫКЛЮЧЕН по умолчанию (PAPER_MONITOR_ENABLED=false);
  - монитор не запускается, если runtime_safety_check() небезопасен;
  - отдельный сторожевой поток перепроверяет конфигурацию в рантайме и
    кооперативно останавливает цикл при обнаружении небезопасного режима;
  - монитор НИКОГДА не бросает исключение в сторону FastAPI: отказ монитора
    не должен ронять /health и /ready;
  - значения секретов не читаются и не попадают в статус.

Реальные ордера здесь невозможны: торговый цикл использует только публичные
данные Binance и paper-исполнение (см. paper_live_loop / live_paper_run).
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

from config.startup_safety import runtime_safety_check


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    return raw.strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_monitor_enabled() -> bool:
    """
    Монитор строго opt-in.

    По умолчанию False: включение по умолчанию изменило бы поведение всех
    существующих тестов и локальных запусков (фоновый поток начал бы ходить
    в сеть при обычном импорте приложения).
    """
    return _env_flag(
        "PAPER_MONITOR_ENABLED",
        default=False,
    )


class PaperMonitor:
    """Супервизор фонового paper-цикла."""

    # Пауза перед перезапуском цикла после неожиданного завершения.
    RESTART_BACKOFF_SECONDS = 30

    # Периодичность рантайм-проверки конфигурации.
    SAFETY_CHECK_INTERVAL_SECONDS = 30

    # Сколько ждём штатной остановки потока при shutdown.
    STOP_TIMEOUT_SECONDS = 20

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._safety_watcher: threading.Thread | None = None

        self._lock = threading.Lock()

        self._state = "STOPPED"
        self._detail: str | None = None
        self._started_at: float | None = None
        self._restart_count = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------

    def _set_state(
        self,
        state: str,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._state = state
            self._detail = detail

    def status(self) -> dict[str, Any]:
        """Статус для эндпоинта. Секретов не содержит."""
        with self._lock:
            state = self._state
            detail = self._detail
            started_at = self._started_at
            restart_count = self._restart_count
            last_error = self._last_error

        uptime = (
            round(time.time() - started_at, 1)
            if started_at is not None
            else None
        )

        return {
            "enabled": is_monitor_enabled(),
            "state": state,
            "detail": detail,
            "running": self.is_running(),
            "uptime_seconds": uptime,
            "restart_count": restart_count,
            "last_error": last_error,
            "real_orders_enabled": False,
            "market_data": "BINANCE_PUBLIC_NO_API_KEY",
        }

    def is_running(self) -> bool:
        worker = self._worker

        return bool(
            worker is not None
            and worker.is_alive()
        )

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """
        Запускает монитор, если он разрешён и конфигурация безопасна.

        Никогда не бросает исключение — возвращает статус. Отказ запуска
        монитора не должен мешать серверу обслуживать /health и /ready.
        """
        if not is_monitor_enabled():
            self._set_state(
                "DISABLED",
                "PAPER_MONITOR_ENABLED is not set to true",
            )
            return self.status()

        safety = runtime_safety_check()

        if not safety["safe"]:
            # Fail-closed: небезопасный режим не запускает торговый цикл.
            self._set_state(
                "REFUSED_UNSAFE",
                str(safety["reason"]),
            )
            print(
                "[PAPER MONITOR] REFUSED TO START: "
                f"{safety['reason']}",
                flush=True,
            )
            return self.status()

        if self.is_running():
            return self.status()

        self._stop_event.clear()

        with self._lock:
            self._started_at = time.time()
            self._last_error = None

        self._worker = threading.Thread(
            target=self._run_supervised,
            name="paper-monitor",
            daemon=True,
        )

        self._safety_watcher = threading.Thread(
            target=self._watch_safety,
            name="paper-monitor-safety",
            daemon=True,
        )

        self._set_state("STARTING")

        self._worker.start()
        self._safety_watcher.start()

        print(
            "[PAPER MONITOR] started "
            "(PAPER only, public market data, no real orders)",
            flush=True,
        )

        return self.status()

    def stop(self) -> dict[str, Any]:
        """Кооперативно останавливает монитор и дожидается потока."""
        self._stop_event.set()

        worker = self._worker

        if worker is not None and worker.is_alive():
            print(
                "[PAPER MONITOR] stopping...",
                flush=True,
            )

            worker.join(
                timeout=self.STOP_TIMEOUT_SECONDS,
            )

            if worker.is_alive():
                # Поток daemon=True, поэтому процесс всё равно завершится.
                # Сообщаем честно, а не делаем вид, что остановка удалась.
                self._set_state(
                    "STOP_TIMEOUT",
                    "worker thread did not stop within "
                    f"{self.STOP_TIMEOUT_SECONDS}s",
                )
                print(
                    "[PAPER MONITOR] WARNING: worker did not stop in time",
                    flush=True,
                )
                return self.status()

        watcher = self._safety_watcher

        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=5)

        self._set_state("STOPPED")

        with self._lock:
            self._started_at = None

        print(
            "[PAPER MONITOR] stopped safely",
            flush=True,
        )

        return self.status()

    # ------------------------------------------------------------------
    # Потоки
    # ------------------------------------------------------------------

    def _watch_safety(self) -> None:
        """
        Рантайм-контроль конфигурации.

        Стартовый гейт отрабатывает один раз при импорте api.server. Этот
        сторож перепроверяет режим и останавливает цикл, если конфигурация
        стала небезопасной, вместо молчаливого продолжения работы.
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(
                self.SAFETY_CHECK_INTERVAL_SECONDS
            ):
                return

            try:
                safety = runtime_safety_check()
            except Exception:
                # Неизвестное состояние проверки не считается безопасным.
                safety = {
                    "safe": False,
                    "reason": "runtime_safety_check failed",
                }

            if not safety["safe"]:
                self._set_state(
                    "STOPPING_UNSAFE",
                    str(safety["reason"]),
                )
                print(
                    "[PAPER MONITOR] FAILED_SAFELY: unsafe configuration "
                    f"detected at runtime: {safety['reason']}",
                    flush=True,
                )
                self._stop_event.set()
                return

    def _run_supervised(self) -> None:
        """
        Держит торговый цикл живым, но не любой ценой.

        Цикл сам обрабатывает ошибки итерации (FAILED_SAFELY) и продолжает
        работу. Сюда управление попадает только при неожиданном выходе —
        тогда делаем паузу, ПЕРЕПРОВЕРЯЕМ безопасность и пробуем снова.
        """
        # Импорт внутри функции: он тянет Bootstrap и провайдеры (~3 с) и
        # не должен выполняться при обычном импорте api.server, когда
        # монитор выключен.
        from paper_live_loop import main as run_paper_loop

        while not self._stop_event.is_set():
            safety = runtime_safety_check()

            if not safety["safe"]:
                self._set_state(
                    "REFUSED_UNSAFE",
                    str(safety["reason"]),
                )
                print(
                    "[PAPER MONITOR] FAILED_SAFELY: "
                    f"{safety['reason']}",
                    flush=True,
                )
                return

            self._set_state("RUNNING")

            try:
                run_paper_loop(
                    stop_event=self._stop_event,
                )

                if self._stop_event.is_set():
                    self._set_state("STOPPED")
                    return

                # Цикл вернул управление сам по себе — это не ожидается.
                self._set_state(
                    "RESTARTING",
                    "paper loop returned unexpectedly",
                )

            except Exception as error:
                detail = (
                    f"{type(error).__name__}: {error}"
                )

                with self._lock:
                    self._last_error = detail

                self._set_state(
                    "RESTARTING",
                    detail,
                )

                print(
                    "[PAPER MONITOR] paper loop crashed; "
                    f"will restart: {detail}",
                    flush=True,
                )
                traceback.print_exc()

            with self._lock:
                self._restart_count += 1

            if self._stop_event.wait(
                self.RESTART_BACKOFF_SECONDS
            ):
                self._set_state("STOPPED")
                return

        self._set_state("STOPPED")


# Единственный экземпляр на процесс.
paper_monitor = PaperMonitor()
