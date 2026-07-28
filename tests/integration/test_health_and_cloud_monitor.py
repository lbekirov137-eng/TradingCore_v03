import time

import pytest
from fastapi.testclient import TestClient

from api.server import app
from api.data_engine import DataEngine
from api.cloud_monitor import CloudMonitor
from api.trade_engine import trade_engine as te
from api.execution.order_reconciler import OrderReconciler

from tests.conftest import orb_breakout_snapshot


client = TestClient(app)


class TestHealthEndpoint:

    def test_health_returns_all_required_fields(self):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()

        for field in (
            "status", "mode", "paper_trading", "live_trading", "demo_only",
            "monitor_running", "last_candle_timestamp_ms", "data_feed_state",
            "open_virtual_positions", "last_cycle_timestamp", "uptime_seconds",
        ):
            assert field in body, f"missing field: {field}"

    def test_health_never_leaks_secret_like_keys(self, monkeypatch):
        monkeypatch.setenv("BYBIT_DEMO_API_KEY", "should-not-appear")
        r = client.get("/health")
        assert "should-not-appear" not in r.text

    def test_health_reports_paper_mode(self):
        body = client.get("/health").json()
        assert body["mode"] == "PAPER"
        assert body["live_trading"] is False

    def test_health_reflects_open_position_count(self):
        from api.position_manager.position_manager import PositionManager

        assert client.get("/health").json()["open_virtual_positions"] == 0

        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="sig-health-test")
        assert client.get("/health").json()["open_virtual_positions"] == 1

        PositionManager.close_position("test cleanup")
        assert client.get("/health").json()["open_virtual_positions"] == 0

    def test_health_reflects_a_completed_tick(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        r = client.get("/paper/tick", params={"replay": "true"})
        assert r.status_code == 200

        body = client.get("/health").json()
        assert body["last_candle_timestamp_ms"] is not None
        assert body["last_cycle_timestamp"] is not None


class TestStrategyStatus:

    def test_both_strategies_marked_research_only_not_production_approved(self):
        body = client.get("/strategies/status").json()

        assert "ORB" in body
        assert "VWAP_TREND_PULLBACK" in body

        for name, info in body.items():
            assert info["status"] == "RESEARCH_ONLY", name
            assert info["production_approved"] is False, name
            assert "version" in info


class TestCloudMonitor:

    def test_starts_and_stops_cleanly(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        reconciler = OrderReconciler(te.broker, te.idempotency_store)
        cm = CloudMonitor()
        cm.loop.reconciler = reconciler
        cm.loop.poll_buffer_seconds = 0.01
        # force a short interval by monkeypatching the wait calculation
        monkeypatch.setattr(cm.loop, "seconds_until_next_close", lambda now=None: 0.01)

        assert cm.is_running() is False

        cm.start()
        time.sleep(0.2)  # let a couple of iterations happen

        assert cm.is_running() is True

        cm.stop()
        time.sleep(0.2)

        assert cm.is_running() is False

    def test_starting_twice_does_not_spawn_a_second_thread(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        cm = CloudMonitor()
        monkeypatch.setattr(cm.loop, "seconds_until_next_close", lambda now=None: 0.5)

        cm.start()
        first_thread = cm._thread

        cm.start()  # idempotent -- must not replace the running thread
        assert cm._thread is first_thread

        cm.stop()
        time.sleep(0.2)

    def test_monitor_never_touches_a_real_exchange_router(self, monkeypatch):
        """
        The monitor's adapter must always be the paper broker -- never
        anything capable of placing a real order.
        """
        cm = CloudMonitor()
        from api.paper_broker.paper_broker import PaperBroker
        assert isinstance(cm.loop.adapter, PaperBroker)
