import json
import os

from api.execution.kill_switch import KillSwitch
from api.contracts.context import LiveContext
from api.decision_engine.decision_engine import DecisionEngine, kill_switch as global_kill_switch


def _approved_signal():
    return {
        "approved": True,
        "strategy": "ORB",
        "direction": "LONG",
        "trade_plan": {
            "entry": 100.0, "stop_loss": 99.0,
            "take_profit": {"tp1": 102.0, "tp2": 103.0, "risk_reward": "1:2 / 1:3"},
        },
        "confidence": 0.5,
        "reason": "ok",
        "metadata": {},
    }


class TestKillSwitchBasics:

    def test_starts_disengaged(self, tmp_path):
        ks = KillSwitch(state_path=str(tmp_path / "ks.json"))
        assert ks.is_engaged() is False

    def test_engage_blocks_and_disengage_restores(self, tmp_path):
        ks = KillSwitch(state_path=str(tmp_path / "ks.json"))
        ks.engage(reason="manual test stop", operator="tester")
        assert ks.is_engaged() is True

        ks.disengage(operator="tester")
        assert ks.is_engaged() is False

    def test_state_survives_new_instance_same_path(self, tmp_path):
        path = str(tmp_path / "ks.json")
        ks1 = KillSwitch(state_path=path)
        ks1.engage(reason="restart-persistence check")

        ks2 = KillSwitch(state_path=path)  # simulates process restart
        assert ks2.is_engaged() is True
        assert ks2.status()["reason"] == "restart-persistence check"

    def test_corrupted_state_file_defaults_to_engaged_not_crash(self, tmp_path):
        path = tmp_path / "ks.json"
        path.write_text("{not valid json at all", encoding="utf-8")

        ks = KillSwitch(state_path=str(path))  # must not raise
        assert ks.is_engaged() is True  # fail closed, not fail open

    def test_close_positions_only_when_explicitly_configured(self, tmp_path):
        ks = KillSwitch(state_path=str(tmp_path / "ks.json"))

        ks.engage(reason="default policy check")
        assert ks.should_close_positions() is False

        ks.engage(reason="explicit close requested", close_positions=True)
        assert ks.should_close_positions() is True

    def test_engage_cancels_pending_orders_best_effort(self, tmp_path):
        cancelled = []

        class FakeAdapter:
            def cancel_order(self, client_order_id):
                cancelled.append(client_order_id)
                return {"client_order_id": client_order_id, "status": "CANCELLED"}

        ks = KillSwitch(state_path=str(tmp_path / "ks.json"))
        result = ks.engage(
            reason="cancel pending test", adapter=FakeAdapter(),
            pending_client_order_ids=["cid-1", "cid-2"],
        )

        assert set(cancelled) == {"cid-1", "cid-2"}
        assert set(result["cancelled_pending_orders"]) == {"cid-1", "cid-2"}

    def test_engage_cancel_failure_does_not_prevent_engagement(self, tmp_path):
        class FailingAdapter:
            def cancel_order(self, client_order_id):
                raise ConnectionError("simulated failure")

        ks = KillSwitch(state_path=str(tmp_path / "ks.json"))
        result = ks.engage(
            reason="cancel-failure test", adapter=FailingAdapter(),
            pending_client_order_ids=["cid-1"],
        )

        assert result["engaged"] is True
        assert ks.is_engaged() is True
        assert result["cancelled_pending_orders"] == []


class TestKillSwitchBlocksNewEntries:

    def test_engaged_kill_switch_forces_no_trade_even_with_good_signal(self):
        global_kill_switch.engage(reason="integration test stop")

        ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
        ctx.strategy_signals = [_approved_signal()]

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "Kill switch" in decision["reason"]

    def test_disengaged_kill_switch_allows_normal_flow(self):
        global_kill_switch.disengage()

        ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
        ctx.strategy_signals = [_approved_signal()]

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "TRADE"
