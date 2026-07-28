"""
Тесты фонового PAPER-монитора.

Проверяют ИМЕННО те инварианты, ради которых монитор так устроен:
  - выключен по умолчанию (иначе обычный импорт приложения полез бы в сеть);
  - отказывается стартовать при небезопасной конфигурации (fail-closed);
  - останавливается кооперативно, а не висит до SIGKILL;
  - останавливается сам, если режим стал небезопасным в рантайме;
  - не роняет сервер, когда торговый цикл падает;
  - не раскрывает секретов в статусе.

Сетевые вызовы и реальный торговый цикл здесь подменяются: тесты обязаны
быть детерминированными и не ходить на биржу.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from api.paper_monitor import PaperMonitor, is_monitor_enabled
from api.server import app


@pytest.fixture(autouse=True)
def _clean_monitor_env(monkeypatch):
    """Каждый тест стартует с безопасной PAPER-конфигурации."""
    monkeypatch.setenv("TRADING_ENVIRONMENT", "PAPER")
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    monkeypatch.delenv("PAPER_MONITOR_ENABLED", raising=False)


class TestMonitorIsOptIn:

    def test_disabled_by_default(self):
        assert is_monitor_enabled() is False

    def test_start_does_nothing_when_disabled(self):
        monitor = PaperMonitor()

        status = monitor.start()

        assert status["enabled"] is False
        assert status["state"] == "DISABLED"
        assert status["running"] is False
        assert monitor.is_running() is False

    def test_enabled_only_by_explicit_true(self, monkeypatch):
        for falsy in ("false", "0", "no", "off", ""):
            monkeypatch.setenv("PAPER_MONITOR_ENABLED", falsy)
            assert is_monitor_enabled() is False, falsy

        for truthy in ("true", "1", "yes", "on", "TRUE"):
            monkeypatch.setenv("PAPER_MONITOR_ENABLED", truthy)
            assert is_monitor_enabled() is True, truthy


class TestMonitorRefusesUnsafeConfiguration:

    def test_refuses_to_start_when_live_trading_is_set(self, monkeypatch):
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "true")
        monkeypatch.setenv("LIVE_TRADING", "true")

        monitor = PaperMonitor()
        status = monitor.start()

        assert status["state"] == "REFUSED_UNSAFE"
        assert status["running"] is False
        assert monitor.is_running() is False

    def test_refuses_to_start_on_live_environment(self, monkeypatch):
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "true")
        monkeypatch.setenv("TRADING_ENVIRONMENT", "LIVE")

        monitor = PaperMonitor()
        status = monitor.start()

        assert status["state"] == "REFUSED_UNSAFE"
        assert monitor.is_running() is False

    def test_refuses_to_start_on_unrecognised_environment(self, monkeypatch):
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "true")
        monkeypatch.setenv("TRADING_ENVIRONMENT", "mainnet")

        monitor = PaperMonitor()
        status = monitor.start()

        assert status["state"] == "REFUSED_UNSAFE"
        assert monitor.is_running() is False


class TestCooperativeStop:

    def test_stops_promptly_instead_of_blocking(self, monkeypatch):
        """
        Ключевой инвариант: остановка должна быть БЫСТРОЙ.

        Голый time.sleep(30) означал бы, что контейнер не успевает
        завершиться по SIGTERM и получает SIGKILL. Цикл ниже спит
        «долго», но через stop_event, поэтому обязан проснуться сразу.
        """
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "true")

        started = threading.Event()

        def fake_loop(stop_event=None):
            started.set()
            # Спим заведомо дольше допустимого времени остановки.
            while not stop_event.wait(30):
                pass

        monkeypatch.setattr(
            "paper_live_loop.main",
            fake_loop,
        )

        monitor = PaperMonitor()
        monitor.start()

        assert started.wait(timeout=10), "цикл не стартовал"
        assert monitor.is_running() is True

        began = time.monotonic()
        status = monitor.stop()
        elapsed = time.monotonic() - began

        assert elapsed < 5, f"остановка заняла {elapsed:.1f}s"
        assert status["state"] == "STOPPED"
        assert monitor.is_running() is False

    def test_stop_is_safe_when_never_started(self):
        monitor = PaperMonitor()

        status = monitor.stop()

        assert status["running"] is False


class TestRuntimeSafetyEnforcement:

    def test_monitor_stops_itself_when_config_becomes_unsafe(
        self, monkeypatch
    ):
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "true")

        started = threading.Event()
        stopped = threading.Event()

        def fake_loop(stop_event=None):
            started.set()
            stop_event.wait()
            stopped.set()

        monkeypatch.setattr("paper_live_loop.main", fake_loop)

        monitor = PaperMonitor()
        # Ускоряем сторожа, чтобы тест не ждал 30 секунд.
        monitor.SAFETY_CHECK_INTERVAL_SECONDS = 0.1

        monitor.start()
        assert started.wait(timeout=10)

        # Конфигурация становится небезопасной уже во время работы.
        monkeypatch.setenv("LIVE_TRADING", "true")

        assert stopped.wait(timeout=10), (
            "монитор не остановился при небезопасной конфигурации"
        )

        monitor.stop()
        assert monitor.is_running() is False


class TestCrashHandling:

    def test_crashing_loop_does_not_raise_out_of_monitor(self, monkeypatch):
        """Падение цикла не должно ронять процесс сервера."""
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "true")

        crashed = threading.Event()

        def exploding_loop(stop_event=None):
            crashed.set()
            raise RuntimeError("simulated loop failure")

        monkeypatch.setattr("paper_live_loop.main", exploding_loop)

        monitor = PaperMonitor()
        monitor.RESTART_BACKOFF_SECONDS = 0.1

        monitor.start()

        assert crashed.wait(timeout=10)

        time.sleep(0.5)

        status = monitor.status()
        assert "simulated loop failure" in str(status["last_error"])

        monitor.stop()


class TestStatusEndpoint:

    def test_monitor_status_endpoint_reports_disabled_by_default(self):
        with TestClient(app) as client:
            response = client.get("/monitor/status")

        assert response.status_code == 200

        body = response.json()
        assert body["enabled"] is False
        assert body["running"] is False
        assert body["real_orders_enabled"] is False
        assert body["market_data"] == "BINANCE_PUBLIC_NO_API_KEY"

    def test_status_never_exposes_secrets(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "should-never-be-echoed")
        monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "sk-never-echoed")

        with TestClient(app) as client:
            text = client.get("/monitor/status").text

        assert "should-never-be-echoed" not in text
        assert "sk-never-echoed" not in text

    def test_existing_health_endpoints_still_work_with_lifespan(self):
        """Добавление lifespan не должно ломать healthcheck Railway."""
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 200
            assert client.get("/safety").status_code == 200
