import time

import pytest

from api.execution.exit_monitor import ExitMonitor, ExitReason
from api.position_manager.position_manager import PositionManager
from api.trade_engine import trade_engine as te


def _open_paper_position(entry=100.0, stop=98.0, tp1=104.0, qty=1.0, opened_at=None):
    """Opens a real position through the paper broker so state is consistent."""

    decision = {
        "decision": "TRADE",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "strategy": "ORB",
        "direction": "LONG",
        "trade_plan": {
            "entry": entry,
            "stop_loss": stop,
            "take_profit": {"tp1": tp1, "tp2": tp1 + (tp1 - entry), "risk_reward": "1:2 / 1:3"},
        },
        "risk": {"position_size": qty, "risk_amount": 1.0},
        "signature": ("binance", "BTCUSDT", "LONG", entry, stop),
        "session_key": ("binance", "BTCUSDT", "ORB", "CRYPTO", 111),
        "reason": "test",
    }

    result = te.TradeEngine.execute(decision)
    assert result["status"] == "OPENED", result

    if opened_at is not None:
        PositionManager.current_position()["opened_at"] = opened_at

    return result


@pytest.fixture
def monitor():
    return ExitMonitor(adapter=te.broker, trade_engine=te.TradeEngine)


class TestExitMonitorBasics:

    def test_no_position_is_safe(self, monitor):
        result = monitor.check({"symbol": "BTCUSDT", "high": 101.0, "low": 99.0, "close": 100.0})
        assert result["action"] == "NO_POSITION"

    def test_holds_when_price_between_stop_and_target(self, monitor):
        _open_paper_position()

        result = monitor.check({"symbol": "BTCUSDT", "high": 101.0, "low": 99.0, "close": 100.0})

        assert result["action"] == "HOLD"
        assert PositionManager.has_open_position() is True

    def test_symbol_mismatch_is_rejected_safely(self, monitor):
        _open_paper_position()

        result = monitor.check({"symbol": "ETHUSDT", "high": 101.0, "low": 99.0, "close": 100.0})

        assert result["action"] == "SYMBOL_MISMATCH"
        assert PositionManager.has_open_position() is True

    def test_invalid_candle_does_not_close_position(self, monitor):
        _open_paper_position()

        result = monitor.check({"symbol": "BTCUSDT", "high": float("nan"), "low": 99.0, "close": 100.0})

        assert result["action"] == "INVALID_CANDLE"
        assert PositionManager.has_open_position() is True


class TestExitTriggers:

    def test_stop_loss_closes_position(self, monitor):
        _open_paper_position(entry=100.0, stop=98.0, tp1=104.0)

        result = monitor.check({"symbol": "BTCUSDT", "high": 99.5, "low": 97.5, "close": 98.0})

        assert result["action"] == "CLOSED"
        assert result["exit_reason"] == ExitReason.STOP_LOSS
        assert PositionManager.has_open_position() is False

    def test_take_profit_closes_position(self, monitor):
        _open_paper_position(entry=100.0, stop=98.0, tp1=104.0)

        result = monitor.check({"symbol": "BTCUSDT", "high": 104.5, "low": 101.0, "close": 104.0})

        assert result["action"] == "CLOSED"
        assert result["exit_reason"] == ExitReason.TAKE_PROFIT_1
        assert PositionManager.has_open_position() is False

    def test_both_stop_and_tp_in_same_candle_picks_conservative_stop(self, monitor):
        """
        EXPLICIT REQUIREMENT: when SL and TP are both inside one candle's
        range, the profitable outcome must NOT be auto-selected. Intrabar
        ordering is unknowable from OHLC, so the worst case (stop) wins.
        """
        _open_paper_position(entry=100.0, stop=98.0, tp1=104.0)

        result = monitor.check({"symbol": "BTCUSDT", "high": 105.0, "low": 97.0, "close": 103.0})

        assert result["action"] == "CLOSED"
        assert result["exit_reason"] == ExitReason.STOP_LOSS  # not TAKE_PROFIT
        assert "консервативный" in result["note"]

    def test_invalidation_closes_position(self, monitor):
        _open_paper_position()

        result = monitor.check(
            {"symbol": "BTCUSDT", "high": 101.0, "low": 99.0, "close": 100.0},
            invalidated=True,
        )

        assert result["action"] == "CLOSED"
        assert result["exit_reason"] == ExitReason.INVALIDATION

    def test_stale_position_is_closed(self, monitor):
        _open_paper_position(opened_at=time.time() - 100_000)

        result = monitor.check({"symbol": "BTCUSDT", "high": 101.0, "low": 99.0, "close": 100.0})

        assert result["action"] == "CLOSED"
        assert result["exit_reason"] == ExitReason.STALE_POSITION


class TestReconciliation:

    def test_broker_flat_syncs_local_state(self, monitor):
        _open_paper_position()

        # Simulate the position disappearing on the exchange side.
        te.broker.positions.clear()

        result = monitor.check({"symbol": "BTCUSDT", "high": 101.0, "low": 99.0, "close": 100.0})

        assert result["action"] == "RECONCILED_FLAT"
        assert PositionManager.has_open_position() is False

    def test_reconcile_failure_does_not_blindly_close(self, monitor):
        _open_paper_position()

        class FailingAdapter:
            def get_position(self, symbol):
                raise ConnectionError("simulated reconciliation failure")

        failing_monitor = ExitMonitor(adapter=FailingAdapter(), trade_engine=te.TradeEngine)

        result = failing_monitor.check({"symbol": "BTCUSDT", "high": 97.0, "low": 96.0, "close": 96.5})

        assert result["action"] == "RECONCILE_FAILED"
        # Position must remain open — a failed reconciliation is not a close signal.
        assert PositionManager.has_open_position() is True


class TestFullRoundTrip:

    def test_open_then_take_profit_produces_positive_realized_pnl(self, monitor):
        _open_paper_position(entry=100.0, stop=98.0, tp1=104.0, qty=1.0)

        starting_pnl = te.broker.get_balance()["realized_pnl"]

        monitor.check({"symbol": "BTCUSDT", "high": 104.5, "low": 101.0, "close": 104.0})

        ending_pnl = te.broker.get_balance()["realized_pnl"]

        assert ending_pnl > starting_pnl
        assert PositionManager.has_open_position() is False

    def test_open_then_stop_loss_produces_negative_realized_pnl(self, monitor):
        _open_paper_position(entry=100.0, stop=98.0, tp1=104.0, qty=1.0)

        monitor.check({"symbol": "BTCUSDT", "high": 99.5, "low": 97.5, "close": 98.0})

        assert te.broker.get_balance()["realized_pnl"] < 0
        assert PositionManager.has_open_position() is False
