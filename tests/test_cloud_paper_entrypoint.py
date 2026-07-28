"""
Smoke tests for the reconciled single cloud entrypoint (uvicorn api.server:app).

Covers the agreed constraints for the first cloud PAPER run:
  - app starts;
  - /health responds;
  - /ready responds and enforces the runtime FAILED_SAFELY rule;
  - live trading is off and structurally refused;
  - leverage is capped at 1x;
  - risk per trade <= 0.1%;
  - no secrets are exposed by any endpoint.
"""

import pytest
from fastapi.testclient import TestClient

from api.server import app
from api.leverage_risk_engine import LeverageRiskEngine


client = TestClient(app)


class TestApplicationStarts:

    def test_root_responds(self):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["mode"] == "PAPER"


class TestHealthEndpoint:

    def test_health_responds_200(self):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_reports_paper_mode_and_live_off(self):
        body = client.get("/health").json()
        assert body["mode"] == "PAPER"
        assert body["live_trading"] is False
        assert body["paper_trading"] is True

    def test_health_exposes_no_secrets(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-echoed")
        monkeypatch.setenv("BYBIT_DEMO_API_KEY", "should-never-be-echoed")

        text = client.get("/health").text
        assert "should-never-be-echoed" not in text
        assert "sk-should-never-be-echoed" not in text


class TestReadyEndpoint:

    def test_ready_is_ready_under_safe_defaults(self):
        r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["status"] == "READY"
        assert body["reasons"] == []

    def test_ready_fails_safely_when_live_trading_env_is_set(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "true")

        r = client.get("/ready")

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "FAILED_SAFELY"
        assert any("unsafe_configuration" in reason for reason in body["reasons"])

    def test_ready_fails_safely_if_leverage_is_raised(self, monkeypatch):
        """A leverage regression must be caught at runtime, not just by unit tests."""
        monkeypatch.setattr(LeverageRiskEngine, "MAX_LEVERAGE", 3)

        r = client.get("/ready")

        assert r.status_code == 503
        assert any("leverage_above_paper_limit" in reason for reason in r.json()["reasons"])

    def test_ready_fails_safely_if_risk_is_raised(self, monkeypatch):
        monkeypatch.setattr(LeverageRiskEngine, "MAX_RISK_PERCENT", 0.05)

        r = client.get("/ready")

        assert r.status_code == 503
        assert any("risk_above_limit" in reason for reason in r.json()["reasons"])


class TestSafetySummary:

    def test_safety_reports_the_agreed_constraints(self):
        body = client.get("/safety").json()

        assert body["live_trading"] is False
        assert body["paper_trading"] is True
        assert body["demo_only"] is True
        assert body["max_leverage"] == 1
        assert body["max_risk_percent"] <= 0.001
        assert body["live_order_code_present"] is False


class TestStrategiesAreResearchOnly:

    def test_no_strategy_is_production_approved(self):
        body = client.get("/strategies/status").json()

        assert body, "expected at least one strategy reported"
        for name, info in body.items():
            assert info["status"] == "RESEARCH_ONLY", name
            assert info["production_approved"] is False, name


class TestStartupGateRefusesUnsafeConfig:
    """
    The gate runs at import time, so it is exercised here through the
    same helper the server uses rather than by re-importing the module.
    """

    def test_live_trading_true_is_refused(self, monkeypatch):
        from config.startup_safety import assert_safe_startup, StartupSafetyError

        monkeypatch.setenv("LIVE_TRADING", "true")

        with pytest.raises(StartupSafetyError):
            assert_safe_startup()

    @pytest.mark.parametrize("bad_env", ["LIVE", "mainnet", "production", "typo"])
    def test_unrecognized_environment_is_refused(self, monkeypatch, bad_env):
        from config.startup_safety import assert_safe_startup, StartupSafetyError

        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.setenv("TRADING_ENVIRONMENT", bad_env)

        with pytest.raises(StartupSafetyError):
            assert_safe_startup()
