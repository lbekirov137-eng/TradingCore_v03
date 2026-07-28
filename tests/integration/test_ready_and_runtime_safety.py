from fastapi.testclient import TestClient

from api.server import app
from api.data_engine import DataEngine
from config.startup_safety import runtime_safety_check

from tests.conftest import orb_breakout_snapshot


client = TestClient(app)


class TestRuntimeSafetyCheck:

    def test_safe_by_default(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.delenv("TRADING_ENVIRONMENT", raising=False)

        result = runtime_safety_check()
        assert result["safe"] is True
        assert result["reason"] is None

    def test_unsafe_when_live_trading_set(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "true")

        result = runtime_safety_check()
        assert result["safe"] is False
        assert "LIVE_TRADING" in result["reason"]

    def test_never_raises_even_when_unsafe(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "mainnet")
        result = runtime_safety_check()  # must not raise
        assert result["safe"] is False


class TestHealthTrackerIsolation:
    """
    Regression test for a real cross-test pollution bug found while adding
    /ready: the module-level `health` singleton (HealthTracker) was never
    reset between tests. A test using 2024-dated fixture candles would
    leave last_market_data_timestamp far in the past, making a LATER
    test's /ready call report stale_market_data for a completely unrelated
    reason -- passing in isolation, failing only in full-suite order.
    """

    def test_health_singleton_is_reset_between_tests_via_old_stale_data(self, monkeypatch):
        from api.observability.states import health

        # Simulate what an earlier test using old fixture data would do.
        health.record_market_data(1_000_000_000_000)  # a timestamp from the deep past

        assert health.status()["market_data_age_seconds"] > 900

        health.reset()

        assert health.status()["market_data_age_seconds"] is None


class TestReadyEndpoint:

    def test_ready_returns_200_by_default(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.delenv("TRADING_ENVIRONMENT", raising=False)

        r = client.get("/ready")

        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["status"] == "READY"
        assert body["reasons"] == []

    def test_ready_returns_503_when_live_trading_env_set(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "true")

        r = client.get("/ready")

        assert r.status_code == 503
        body = r.json()
        assert body["ready"] is False
        assert body["status"] == "FAILED_SAFELY"
        assert any("unsafe_configuration" in reason for reason in body["reasons"])

    def test_ready_never_leaks_secrets(self, monkeypatch):
        monkeypatch.setenv("BYBIT_DEMO_API_KEY", "should-not-appear-in-ready")
        r = client.get("/ready")
        assert "should-not-appear-in-ready" not in r.text


class TestSchedulerLoopSelfStopsOnUnsafeConfig:

    def test_run_once_stops_the_loop_when_config_becomes_unsafe(self, monkeypatch):
        from api.scheduler.loop import SchedulerLoop
        from api.trade_engine import trade_engine as te
        from api.execution.order_reconciler import OrderReconciler

        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))
        monkeypatch.setenv("LIVE_TRADING", "true")

        reconciler = OrderReconciler(te.broker, te.idempotency_store)
        loop = SchedulerLoop(
            exchange="binance", symbol="BTCUSDT", interval="5m", limit=300,
            adapter=te.broker, trade_engine=te.TradeEngine, reconciler=reconciler,
        )

        result = loop.run_once()

        assert result.get("failed_safely") is True
        assert loop.is_stopping() is True

    def test_run_once_proceeds_normally_when_config_is_safe(self, monkeypatch):
        from api.scheduler.loop import SchedulerLoop
        from api.trade_engine import trade_engine as te
        from api.execution.order_reconciler import OrderReconciler

        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        reconciler = OrderReconciler(te.broker, te.idempotency_store)
        loop = SchedulerLoop(
            exchange="binance", symbol="BTCUSDT", interval="5m", limit=300,
            adapter=te.broker, trade_engine=te.TradeEngine, reconciler=reconciler,
            replay_mode=True,
        )

        result = loop.run_once()

        assert result.get("failed_safely") is None
        assert loop.is_stopping() is False
