import time

from api.risk.guards import (
    LossStreakGuard,
    CooldownAfterLossGuard,
    MaxDrawdownGuard,
    MaxTradesPerSessionGuard,
    DailyLossGuard,
    MaxOpenPositionsGuard,
    reset_all,
)


class TestLossStreakGuard:

    def test_allows_when_no_losses(self):
        assert LossStreakGuard.check(max_consecutive_losses=3)["allowed"] is True

    def test_blocks_after_n_consecutive_losses(self):
        for _ in range(3):
            LossStreakGuard.register_result(-1.0)
        result = LossStreakGuard.check(max_consecutive_losses=3)
        assert result["allowed"] is False
        assert result["guard"] == "LossStreakGuard"

    def test_a_win_resets_the_streak(self):
        LossStreakGuard.register_result(-1.0)
        LossStreakGuard.register_result(-1.0)
        LossStreakGuard.register_result(2.0)  # win resets
        LossStreakGuard.register_result(-1.0)
        assert LossStreakGuard.check(max_consecutive_losses=3)["allowed"] is True

    def test_breakeven_does_not_reset_or_count(self):
        LossStreakGuard.register_result(-1.0)
        LossStreakGuard.register_result(0.0)  # neither win nor loss
        LossStreakGuard.register_result(-1.0)
        LossStreakGuard.register_result(-1.0)
        assert LossStreakGuard.check(max_consecutive_losses=3)["allowed"] is False


class TestCooldownAfterLossGuard:

    def test_allows_when_no_loss_yet(self):
        assert CooldownAfterLossGuard.check(cooldown_seconds=3600)["allowed"] is True

    def test_blocks_immediately_after_a_loss(self):
        CooldownAfterLossGuard.register_result(-1.0)
        result = CooldownAfterLossGuard.check(cooldown_seconds=3600)
        assert result["allowed"] is False
        assert "осталось" in result["reason"]

    def test_a_win_does_not_trigger_cooldown(self):
        CooldownAfterLossGuard.register_result(2.0)
        assert CooldownAfterLossGuard.check(cooldown_seconds=3600)["allowed"] is True

    def test_allows_again_after_cooldown_elapses(self):
        CooldownAfterLossGuard.register_result(-1.0)
        assert CooldownAfterLossGuard.check(cooldown_seconds=0.01)["allowed"] is False
        time.sleep(0.02)
        assert CooldownAfterLossGuard.check(cooldown_seconds=0.01)["allowed"] is True


class TestMaxDrawdownGuard:

    def test_allows_with_no_history(self):
        assert MaxDrawdownGuard.check(equity=1000.0, max_drawdown_percent=5.0)["allowed"] is True

    def test_tracks_peak_and_blocks_on_drawdown(self):
        MaxDrawdownGuard.register_equity(1000.0)
        MaxDrawdownGuard.register_equity(1100.0)  # new peak
        result = MaxDrawdownGuard.check(equity=1040.0, max_drawdown_percent=5.0)  # ~5.45% down from peak
        assert result["allowed"] is False

    def test_allows_within_drawdown_tolerance(self):
        MaxDrawdownGuard.register_equity(1000.0)
        result = MaxDrawdownGuard.check(equity=980.0, max_drawdown_percent=5.0)  # 2% down
        assert result["allowed"] is True

    def test_none_equity_is_safe_noop(self):
        assert MaxDrawdownGuard.check(equity=None)["allowed"] is True
        MaxDrawdownGuard.register_equity(None)  # must not raise


class TestMaxTradesPerSessionGuard:

    def test_allows_first_trade_in_session(self):
        result = MaxTradesPerSessionGuard.check("session-A", max_trades_per_session=1)
        assert result["allowed"] is True

    def test_blocks_second_trade_in_same_session(self):
        MaxTradesPerSessionGuard.register_trade("session-A")
        result = MaxTradesPerSessionGuard.check("session-A", max_trades_per_session=1)
        assert result["allowed"] is False

    def test_different_session_is_independent(self):
        MaxTradesPerSessionGuard.register_trade("session-A")
        result = MaxTradesPerSessionGuard.check("session-B", max_trades_per_session=1)
        assert result["allowed"] is True

    def test_none_session_key_is_safe_noop(self):
        assert MaxTradesPerSessionGuard.check(None)["allowed"] is True


class TestDailyLossGuard:

    def test_allows_when_no_losses_today(self):
        assert DailyLossGuard.check(balance=1000.0, max_daily_loss_percent=2.0)["allowed"] is True

    def test_blocks_when_realized_loss_exceeds_limit(self):
        DailyLossGuard.register_result(-25.0)  # 2.5% of 1000
        result = DailyLossGuard.check(balance=1000.0, max_daily_loss_percent=2.0)
        assert result["allowed"] is False
        assert result["guard"] == "DailyLossGuard"

    def test_allows_within_limit(self):
        DailyLossGuard.register_result(-10.0)  # 1% of 1000
        result = DailyLossGuard.check(balance=1000.0, max_daily_loss_percent=2.0)
        assert result["allowed"] is True

    def test_wins_do_not_count_against_the_limit(self):
        DailyLossGuard.register_result(50.0)
        DailyLossGuard.register_result(-10.0)
        result = DailyLossGuard.check(balance=1000.0, max_daily_loss_percent=2.0)
        assert result["allowed"] is True

    def test_is_distinct_from_planned_risk_daily_guard(self):
        """
        DailyLossGuard tracks REALIZED pnl from closed trades, unlike
        DailyRiskGuard (api/risk_engine.py) which tracks planned risk at
        open time. A trade that opens (planned risk) but never closes
        must not affect DailyLossGuard at all.
        """
        # No register_result call at all -- simulating an open, unresolved trade.
        assert DailyLossGuard.check(balance=1000.0, max_daily_loss_percent=2.0)["allowed"] is True


class TestMaxOpenPositionsGuard:

    def test_allows_when_no_position_open(self):
        assert MaxOpenPositionsGuard.check()["allowed"] is True

    def test_blocks_when_position_open(self):
        from api.position_manager.position_manager import PositionManager
        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="sig-1")

        result = MaxOpenPositionsGuard.check()
        assert result["allowed"] is False
        assert result["guard"] == "MaxOpenPositionsGuard"


class TestResetAll:

    def test_reset_all_clears_every_guard(self):
        LossStreakGuard.register_result(-1.0)
        CooldownAfterLossGuard.register_result(-1.0)
        MaxDrawdownGuard.register_equity(1000.0)
        MaxTradesPerSessionGuard.register_trade("session-A")
        DailyLossGuard.register_result(-100.0)

        reset_all()

        assert LossStreakGuard.check(max_consecutive_losses=1)["allowed"] is True
        assert CooldownAfterLossGuard.check(cooldown_seconds=3600)["allowed"] is True
        assert MaxDrawdownGuard.check(equity=1.0, max_drawdown_percent=1.0)["allowed"] is True
        assert MaxTradesPerSessionGuard.check("session-A", max_trades_per_session=1)["allowed"] is True
        assert DailyLossGuard.check(balance=1000.0, max_daily_loss_percent=0.01)["allowed"] is True
