"""
Оба entrypoint обязаны проходить ОДИН startup safety gate.

Дефект, который закрывают эти тесты: api/main.py создавал собственное
FastAPI-приложение без гейта. Команда `uvicorn api.main:app` поднималась
при LIVE_TRADING=true, не имела /ready и /safety, а /health безусловно
отвечала "healthy". Смена модуля в start command обходила всю защиту
облачного entrypoint.

Тесты сравнивают api.server:app и api.main:app по одним и тем же
инвариантам, поэтому расхождение защиты между ними сломает сборку.
"""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

import api.main
import api.server


ENTRYPOINTS = ("api.server", "api.main")


@pytest.fixture(autouse=True)
def _safe_env(monkeypatch):
    monkeypatch.setenv("TRADING_ENVIRONMENT", "PAPER")
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    monkeypatch.delenv("PAPER_MONITOR_ENABLED", raising=False)


def app_of(module_name: str):
    return importlib.import_module(module_name).app


@pytest.mark.parametrize("module_name", ENTRYPOINTS)
class TestSafeConfiguration:

    def test_health_is_200_and_reports_paper_mode(self, module_name):
        with TestClient(app_of(module_name)) as client:
            response = client.get("/health")

        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "HEALTHY"
        assert body["mode"] == "PAPER"
        assert body["live_trading"] is False
        assert body["paper_trading"] is True
        assert body["demo_only"] is True

    def test_ready_is_200_under_safe_defaults(self, module_name):
        with TestClient(app_of(module_name)) as client:
            response = client.get("/ready")

        assert response.status_code == 200

        body = response.json()
        assert body["ready"] is True
        assert body["status"] == "READY"
        assert body["reasons"] == []

    def test_safety_reports_the_agreed_constraints(self, module_name):
        with TestClient(app_of(module_name)) as client:
            body = client.get("/safety").json()

        assert body["trading_environment"] == "PAPER"
        assert body["live_trading"] is False
        assert body["paper_trading"] is True
        assert body["demo_only"] is True
        assert body["max_leverage"] == 1
        assert body["max_risk_percent"] <= 0.001
        assert body["live_order_code_present"] is False

    def test_monitor_status_is_exposed(self, module_name):
        with TestClient(app_of(module_name)) as client:
            response = client.get("/monitor/status")

        assert response.status_code == 200
        assert response.json()["real_orders_enabled"] is False


@pytest.mark.parametrize("module_name", ENTRYPOINTS)
class TestMonitorStaysOff:

    def test_monitor_is_disabled_when_flag_is_absent(self, module_name):
        with TestClient(app_of(module_name)) as client:
            body = client.get("/monitor/status").json()

        assert body["enabled"] is False
        assert body["running"] is False
        assert body["state"] == "DISABLED"

    def test_monitor_is_disabled_when_flag_is_false(
        self, module_name, monkeypatch
    ):
        monkeypatch.setenv("PAPER_MONITOR_ENABLED", "false")

        with TestClient(app_of(module_name)) as client:
            body = client.get("/monitor/status").json()

        assert body["enabled"] is False
        assert body["running"] is False

    def test_no_trading_loop_thread_is_started(self, module_name):
        """Ни один entrypoint не поднимает торговый цикл сам по себе."""
        import threading

        before = {t.name for t in threading.enumerate()}

        with TestClient(app_of(module_name)) as client:
            client.get("/health")
            during = {t.name for t in threading.enumerate()}

        assert "paper-monitor" not in during - before

    def test_graceful_shutdown_leaves_monitor_stopped(self, module_name):
        from api.paper_monitor import paper_monitor

        with TestClient(app_of(module_name)) as client:
            client.get("/health")

        # После выхода из контекста lifespan уже отработал shutdown.
        assert paper_monitor.is_running() is False


@pytest.mark.parametrize("module_name", ENTRYPOINTS)
class TestUnsafeConfigurationIsRefused:
    """
    Ключевая проверка: небезопасный режим не должен проходить НИ ЧЕРЕЗ
    ОДИН entrypoint. Модуль перезагружается, потому что гейт срабатывает
    именно при импорте.
    """

    def test_live_trading_true_refuses_import(
        self, module_name, monkeypatch
    ):
        monkeypatch.setenv("LIVE_TRADING", "true")

        from config.startup_safety import StartupSafetyError

        module = sys.modules[module_name]

        with pytest.raises(StartupSafetyError):
            importlib.reload(module)

        # Возвращаем модуль в рабочее состояние для остальных тестов.
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        importlib.reload(module)

    def test_live_environment_refuses_import(
        self, module_name, monkeypatch
    ):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "LIVE")

        from config.startup_safety import StartupSafetyError

        module = sys.modules[module_name]

        with pytest.raises(StartupSafetyError):
            importlib.reload(module)

        monkeypatch.setenv("TRADING_ENVIRONMENT", "PAPER")
        importlib.reload(module)

    def test_unrecognised_environment_refuses_import(
        self, module_name, monkeypatch
    ):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "mainnet")

        from config.startup_safety import StartupSafetyError

        module = sys.modules[module_name]

        with pytest.raises(StartupSafetyError):
            importlib.reload(module)

        monkeypatch.setenv("TRADING_ENVIRONMENT", "PAPER")
        importlib.reload(module)

    def test_ready_fails_safely_when_live_flag_appears_at_runtime(
        self, module_name, monkeypatch
    ):
        monkeypatch.setenv("LIVE_TRADING", "true")

        with TestClient(app_of(module_name)) as client:
            response = client.get("/ready")

        assert response.status_code == 503

        body = response.json()
        assert body["ready"] is False
        assert body["status"] == "FAILED_SAFELY"
        assert any("unsafe_configuration" in r for r in body["reasons"])


@pytest.mark.parametrize("module_name", ENTRYPOINTS)
class TestNoSecretsAndNoOrders:

    def test_endpoints_never_echo_secrets(self, module_name, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "should-never-be-echoed")
        monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "sk-never-echoed")

        with TestClient(app_of(module_name)) as client:
            text = "".join(
                client.get(path).text
                for path in ("/health", "/ready", "/safety", "/monitor/status")
            )

        assert "should-never-be-echoed" not in text
        assert "sk-never-echoed" not in text

    def test_real_orders_are_reported_impossible(self, module_name):
        with TestClient(app_of(module_name)) as client:
            assert (
                client.get("/safety").json()["live_order_code_present"]
                is False
            )
            assert (
                client.get("/monitor/status").json()["real_orders_enabled"]
                is False
            )


class TestSingleSharedImplementation:
    """
    Защита обязана быть ОДНОЙ реализацией, а не двумя похожими.
    Эти проверки ломаются, если кто-то снова заведёт локальный гейт.
    """

    def test_both_entrypoints_use_the_shared_gate(self):
        from api import app_safety

        assert api.server.enforce_startup_safety is (
            app_safety.enforce_startup_safety
        )
        assert api.main.enforce_startup_safety is (
            app_safety.enforce_startup_safety
        )

    def test_both_entrypoints_use_the_shared_lifespan(self):
        from api import app_safety

        assert api.server.paper_monitor_lifespan is (
            app_safety.paper_monitor_lifespan
        )
        assert api.main.paper_monitor_lifespan is (
            app_safety.paper_monitor_lifespan
        )

    def test_safety_routes_are_identical_on_both_apps(self):
        required = {"/health", "/ready", "/safety", "/monitor/status"}

        server_paths = {r.path for r in api.server.app.routes}
        main_paths = {r.path for r in api.main.app.routes}

        assert required <= server_paths
        assert required <= main_paths

    def test_safety_payloads_match_between_entrypoints(self):
        with TestClient(api.server.app) as server_client:
            server_safety = server_client.get("/safety").json()

        with TestClient(api.main.app) as main_client:
            main_safety = main_client.get("/safety").json()

        assert server_safety == main_safety
