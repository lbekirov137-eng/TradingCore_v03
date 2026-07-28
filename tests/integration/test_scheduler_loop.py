import pytest

from api.scheduler.loop import SchedulerLoop
from api.data_engine import DataEngine
from api.trade_engine import trade_engine as te
from api.position_manager.position_manager import PositionManager
from api.execution.order_reconciler import OrderReconciler
from api.observability.states import health, logger

from tests.conftest import orb_breakout_snapshot


def _loop(replay_mode=True):
    # replay_mode=True by default here: these tests drive the loop with
    # deterministic historical fixtures (tests/conftest.py), so the stale-data
    # filter must measure "now" from the fixture's own timestamps, not the
    # wall clock -- exactly like the e2e tests. Production use leaves this False.
    reconciler = OrderReconciler(te.broker, te.idempotency_store)
    return SchedulerLoop(
        exchange="binance", symbol="BTCUSDT", interval="5m", limit=300,
        adapter=te.broker, trade_engine=te.TradeEngine, reconciler=reconciler,
        replay_mode=replay_mode,
    )


class TestRunOnce:

    def test_no_trade_tick_updates_health_and_logs(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        loop = _loop()
        result = loop.run_once()

        assert result["decision"]["decision"] == "NO_TRADE"
        assert health.last_heartbeat is not None
        assert health.last_market_data_timestamp is not None

    def test_good_signal_opens_position_via_loop(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

        loop = _loop()
        result = loop.run_once()

        assert result["execution"]["status"] == "OPENED"
        assert PositionManager.has_open_position() is True

    def test_exit_monitor_is_checked_every_tick(self, monkeypatch):
        """
        After a position opens, a subsequent tick whose candle range touches
        the stop must close it automatically -- without any manual close call.
        """
        snapshot = orb_breakout_snapshot(breakout=True)
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: snapshot))

        loop = _loop()
        first = loop.run_once()
        assert first["execution"]["status"] == "OPENED"

        position = PositionManager.current_position()
        stop = position["stop"]

        # Next tick: candle range dips to the stop price.
        hit_stop_snapshot = orb_breakout_snapshot(breakout=True)
        hit_stop_snapshot.highs[-1] = position["entry"] + 0.1
        hit_stop_snapshot.lows[-1] = stop - 0.5
        hit_stop_snapshot.closes[-1] = stop
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: hit_stop_snapshot))

        second = loop.run_once()

        assert second["exit"]["action"] == "CLOSED"
        assert PositionManager.has_open_position() is False

    def test_data_error_is_already_handled_safely_by_scheduler_tick(self, monkeypatch):
        """
        Scheduler.tick() already has its own safety boundary around data/network
        errors (see api/scheduler/scheduler.py) and returns a safe NO_TRADE
        decision rather than raising. run_once() must surface that safe
        outcome cleanly -- not crash, not double-report an "error".
        """
        def _raise(**kw):
            raise RuntimeError("simulated catastrophic failure")

        monkeypatch.setattr(DataEngine, "load", staticmethod(_raise))

        loop = _loop()
        result = loop.run_once()  # must not raise

        assert result["decision"]["decision"] == "NO_TRADE"
        assert "simulated catastrophic failure" in result["decision"]["reason"]

    def test_error_outside_scheduler_tick_is_caught_by_the_loop_itself(self, monkeypatch):
        """
        Tests SchedulerLoop's OWN outer safety net (distinct from
        Scheduler.tick's), by breaking something downstream of the decision
        (Workflow.run itself) that isn't already guarded.
        """
        from api.workflow import workflow as wf_module

        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        def _raise(context):
            raise RuntimeError("simulated failure downstream of Scheduler.tick")

        monkeypatch.setattr(wf_module.Workflow, "run", staticmethod(_raise))

        loop = _loop()
        result = loop.run_once()  # must not raise

        assert "error" in result
        assert "simulated failure downstream" in result["error"]
        assert health.last_error is not None


class TestRunForever:

    def test_stops_after_max_iterations(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        loop = _loop()
        result = loop.run_forever(max_iterations=3, sleep_fn=lambda s: None)

        assert result["iterations"] == 3

    def test_stop_called_mid_run_halts_loop(self, monkeypatch):
        monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

        loop = _loop()
        call_count = {"n": 0}

        original_run_once = loop.run_once

        def counting_run_once():
            call_count["n"] += 1
            if call_count["n"] >= 2:
                loop.stop()
            return original_run_once()

        loop.run_once = counting_run_once

        result = loop.run_forever(sleep_fn=lambda s: None)  # no max_iterations — relies on stop()

        assert result["iterations"] == 2
        assert loop.is_stopping() is True

    def test_error_in_one_iteration_does_not_stop_subsequent_iterations(self, monkeypatch):
        calls = {"n": 0}

        def flaky_load(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("transient outage")
            return orb_breakout_snapshot(breakout=False)

        monkeypatch.setattr(DataEngine, "load", staticmethod(flaky_load))

        loop = _loop()
        result = loop.run_forever(max_iterations=3, sleep_fn=lambda s: None)

        assert result["iterations"] == 3
        assert calls["n"] == 3


class TestCandleCloseScheduling:

    def test_seconds_until_next_close_is_within_interval_plus_buffer(self):
        loop = _loop()
        wait = loop.seconds_until_next_close(now=1_700_000_000.123)

        interval_seconds = 5 * 60
        assert 0 < wait <= interval_seconds + loop.poll_buffer_seconds

    def test_wait_time_shrinks_as_candle_close_approaches(self):
        loop = _loop()

        # Both points chosen strictly within the same 5-minute interval
        # (not straddling a boundary), so the wait time monotonically shrinks.
        interval_seconds = 5 * 60
        base = (1_700_000_000 // interval_seconds) * interval_seconds  # aligned interval start

        wait_early = loop.seconds_until_next_close(now=base + 10)
        wait_later = loop.seconds_until_next_close(now=base + 200)

        assert wait_later < wait_early
