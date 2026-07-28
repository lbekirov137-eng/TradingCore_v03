import pytest

from config.startup_safety import (
    assert_safe_startup,
    build_startup_summary,
    get_trading_environment,
    get_live_trading_flag,
    get_paper_trading_flag,
    get_demo_only_flag,
    StartupSafetyError,
)


class TestFlagParsing:

    def test_defaults_are_safe_with_no_env_vars(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.delenv("PAPER_TRADING", raising=False)
        monkeypatch.delenv("DEMO_ONLY", raising=False)
        monkeypatch.delenv("TRADING_ENVIRONMENT", raising=False)

        assert get_live_trading_flag() is False
        assert get_paper_trading_flag() is True
        assert get_demo_only_flag() is True
        assert get_trading_environment() == "PAPER"

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on"])
    def test_truthy_live_trading_values_detected(self, monkeypatch, value):
        monkeypatch.setenv("LIVE_TRADING", value)
        assert get_live_trading_flag() is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", ""])
    def test_falsy_live_trading_values_stay_safe(self, monkeypatch, value):
        monkeypatch.setenv("LIVE_TRADING", value)
        assert get_live_trading_flag() is False


class TestAssertSafeStartup:

    def test_passes_with_default_paper_environment(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.delenv("TRADING_ENVIRONMENT", raising=False)

        summary = assert_safe_startup()

        assert summary["live_trading"] is False
        assert summary["trading_environment"] == "PAPER"

    def test_passes_with_demo_environment(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "DEMO")
        monkeypatch.delenv("LIVE_TRADING", raising=False)

        summary = assert_safe_startup()
        assert summary["trading_environment"] == "DEMO"

    def test_refuses_when_live_trading_flag_is_true(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING", "true")

        with pytest.raises(StartupSafetyError, match="LIVE_TRADING"):
            assert_safe_startup()

    def test_refuses_when_environment_is_live(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.setenv("TRADING_ENVIRONMENT", "LIVE")

        with pytest.raises(StartupSafetyError, match="LIVE"):
            assert_safe_startup()

    @pytest.mark.parametrize("garbage", ["mainnet", "production", "prod", "Live ", "unknown", "papper"])
    def test_refuses_unknown_environment_values(self, monkeypatch, garbage):
        monkeypatch.delenv("LIVE_TRADING", raising=False)
        monkeypatch.setenv("TRADING_ENVIRONMENT", garbage)

        with pytest.raises(StartupSafetyError):
            assert_safe_startup()

    def test_live_trading_flag_wins_even_with_valid_environment(self, monkeypatch):
        """
        A misconfigured combination (valid TRADING_ENVIRONMENT but
        LIVE_TRADING=true) must still refuse -- no single correct-looking
        variable should be able to offset a dangerous one.
        """
        monkeypatch.setenv("TRADING_ENVIRONMENT", "PAPER")
        monkeypatch.setenv("LIVE_TRADING", "true")

        with pytest.raises(StartupSafetyError):
            assert_safe_startup()


class TestStartupSummaryNeverLeaksSecrets:

    def test_summary_contains_no_secret_like_keys(self, monkeypatch):
        monkeypatch.setenv("BYBIT_DEMO_API_KEY", "should-never-appear")
        monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "should-never-appear-either")

        summary = build_startup_summary()

        serialized = str(summary)
        assert "should-never-appear" not in serialized
        assert "API_KEY" not in serialized
        assert "SECRET" not in serialized
