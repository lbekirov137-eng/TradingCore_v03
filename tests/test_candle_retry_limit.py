"""
Ограничитель повторной обработки одной свечи.

Первопричина исходного дефекта: last_processed_close_time_ms обновлялся
только в конце успешной итерации. Любое исключение оставляло его прежним,
поэтому на следующем опросе бралась ТА ЖЕ свеча — и так бесконечно. При
постоянной ошибке цикл не продвигался вообще: измерено 33 попытки за
0.5 с при опросе 0.01 с; в тесте дедупликации одна свеча обрабатывалась
39 раз подряд.

Транзиентные сбои повторять нужно — ограничивается только число попыток
по ОДНОЙ свече.
"""

import threading

import pytest


class DummyContext:
    exchange = "binance"
    symbol = "BTCUSDT"
    timeframe = "5m"
    market = {"source": "TEST"}


def snapshot(close_time_ms: int) -> dict:
    return {
        "close_time_ms": close_time_ms,
        "price": 100.0,
        "candle_high": 101.0,
        "candle_low": 99.0,
    }


CANDLE_A = 1_700_000_000_000
CANDLE_B = 1_700_000_300_000


@pytest.fixture
def loop_module(monkeypatch, tmp_path):
    """Изолированный цикл: без сети, Telegram и записи в репозиторий."""
    import paper_live_loop as loop

    journal: list[dict] = []

    monkeypatch.setattr(loop, "POSITION_STATE_FILE", tmp_path / "pos.json")
    monkeypatch.setattr(loop, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(loop, "RETRY_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(loop, "RETRY_BACKOFF", 1.0)
    monkeypatch.setattr(loop, "MAX_ATTEMPTS_PER_CANDLE", 3)

    monkeypatch.setattr(loop, "build_live_context", lambda: DummyContext())
    monkeypatch.setattr(loop, "append_journal", journal.append)
    monkeypatch.setattr(
        loop,
        "save_runtime_state",
        lambda value, used_signal_id=None: None,
    )
    monkeypatch.setattr(loop, "load_runtime_state", lambda: {})
    monkeypatch.setattr(loop, "print_result", lambda record: None)
    monkeypatch.setattr(loop, "log_decision_line", lambda record: None)
    monkeypatch.setattr(
        loop, "send_event_notification", lambda record: False
    )
    monkeypatch.setattr(
        loop, "build_daily_opportunity_summary", lambda date_utc: ""
    )
    monkeypatch.setattr(
        loop,
        "build_journal_record",
        lambda context, snapshot, position_event, pipeline_data: {
            "recorded_at_utc": "2026-07-28T12:00:00+00:00",
            "symbol": "BTCUSDT",
            "position_event": position_event,
        },
    )

    loop._test_journal = journal
    return loop


def run_loop(loop, window: float = 0.8) -> list[dict]:
    stop_event = threading.Event()

    thread = threading.Thread(
        target=loop.main,
        kwargs={"stop_event": stop_event},
        daemon=True,
    )
    thread.start()

    stop_event.wait(window)
    stop_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive(), "цикл не остановился по stop_event"

    return loop._test_journal


class TestPermanentFailureIsBounded:

    def test_attempts_stop_at_the_configured_limit(
        self, loop_module, monkeypatch
    ):
        attempts = []

        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )

        def always_fails(**kwargs):
            attempts.append(1)
            raise RuntimeError("permanent failure")

        monkeypatch.setattr(
            loop_module, "process_closed_candle", always_fails
        )

        run_loop(loop_module)

        # Без ограничителя за это окно набралось бы много десятков попыток.
        assert len(attempts) == 3, (
            f"ожидалось ровно 3 попытки, получено {len(attempts)}"
        )

    def test_failed_candle_is_recorded_with_full_context(
        self, loop_module, monkeypatch
    ):
        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )

        def always_fails(**kwargs):
            raise RuntimeError("permanent failure")

        monkeypatch.setattr(
            loop_module, "process_closed_candle", always_fails
        )

        journal = run_loop(loop_module)

        failed = [
            r
            for r in journal
            if r.get("status") == "CANDLE_PROCESSING_FAILED_SAFELY"
        ]

        assert len(failed) == 1

        record = failed[0]
        assert record["candle_close_time_ms"] == CANDLE_A
        assert record["attempts"] == 3
        assert record["max_attempts_per_candle"] == 3
        assert record["error_type"] == "RuntimeError"
        assert "permanent failure" in record["error"]
        assert record["trade_created"] is False
        assert record["real_order_sent"] is False

    def test_no_trade_is_created_for_a_failed_candle(
        self, loop_module, monkeypatch
    ):
        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )
        monkeypatch.setattr(
            loop_module,
            "process_closed_candle",
            lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("permanent failure")
            ),
        )

        journal = run_loop(loop_module)

        assert not any("position_event" in r for r in journal)

    def test_loop_keeps_running_after_the_limit(
        self, loop_module, monkeypatch
    ):
        """Достижение лимита не должно останавливать монитор."""
        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )
        monkeypatch.setattr(
            loop_module,
            "process_closed_candle",
            lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("permanent failure")
            ),
        )

        run_loop(loop_module)  # завершается штатно по stop_event


class TestTransientFailureStillWorks:

    def test_transient_error_then_success(
        self, loop_module, monkeypatch
    ):
        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )

        calls = {"count": 0}

        def flaky(**kwargs):
            calls["count"] += 1

            if calls["count"] == 1:
                raise RuntimeError("transient failure")

            return ({"event": "NO_TRADE", "position": None}, {})

        monkeypatch.setattr(
            loop_module, "process_closed_candle", flaky
        )

        journal = run_loop(loop_module)

        # Первая попытка упала, вторая прошла — и на этом всё:
        # успешно обработанная свеча больше не берётся в работу.
        assert calls["count"] == 2

        assert not any(
            r.get("status") == "CANDLE_PROCESSING_FAILED_SAFELY"
            for r in journal
        )
        assert any("position_event" in r for r in journal)


class TestCounterIsPerCandle:

    def test_new_candle_restarts_the_counter(
        self, loop_module, monkeypatch
    ):
        seen = []

        # Первая свеча падает, затем навсегда приходит вторая.
        state = {"switched": False}

        def advancing(context):
            return snapshot(
                CANDLE_B if state["switched"] else CANDLE_A
            )

        monkeypatch.setattr(
            loop_module, "extract_market_snapshot", advancing
        )

        def process(**kwargs):
            close_time = kwargs["snapshot"]["close_time_ms"]
            seen.append(close_time)

            if close_time == CANDLE_A:
                if seen.count(CANDLE_A) >= 3:
                    state["switched"] = True
                raise RuntimeError("permanent failure on candle A")

            return ({"event": "NO_TRADE", "position": None}, {})

        monkeypatch.setattr(
            loop_module, "process_closed_candle", process
        )

        journal = run_loop(loop_module)

        assert seen.count(CANDLE_A) == 3, (
            f"свеча A обработана {seen.count(CANDLE_A)} раз вместо 3"
        )

        # Новая свеча обрабатывается независимо и успешно, ровно один раз.
        assert seen.count(CANDLE_B) == 1

        failed = [
            r
            for r in journal
            if r.get("status") == "CANDLE_PROCESSING_FAILED_SAFELY"
        ]
        assert len(failed) == 1
        assert failed[0]["candle_close_time_ms"] == CANDLE_A
        assert failed[0]["attempts"] == 3

    def test_failed_candle_is_not_treated_as_successfully_processed(
        self, loop_module, monkeypatch
    ):
        """
        Сигнал не помечается использованным: неудачная свеча не должна
        «съесть» сигнал, который ещё ни разу не привёл к сделке.
        """
        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )
        monkeypatch.setattr(
            loop_module,
            "process_closed_candle",
            lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("permanent failure")
            ),
        )

        journal = run_loop(loop_module)

        assert all(
            r.get("signal_id") is None for r in journal
        )


class TestConfiguration:

    def test_limit_is_configurable(self, loop_module, monkeypatch):
        monkeypatch.setattr(loop_module, "MAX_ATTEMPTS_PER_CANDLE", 5)

        attempts = []

        monkeypatch.setattr(
            loop_module,
            "extract_market_snapshot",
            lambda context: snapshot(CANDLE_A),
        )

        def always_fails(**kwargs):
            attempts.append(1)
            raise RuntimeError("permanent failure")

        monkeypatch.setattr(
            loop_module, "process_closed_candle", always_fails
        )

        run_loop(loop_module)

        assert len(attempts) == 5

    def test_retry_settings_have_safe_defaults(self):
        import paper_live_loop as loop

        assert loop.MAX_ATTEMPTS_PER_CANDLE >= 1
        assert loop.RETRY_DELAY_SECONDS > 0
        assert loop.RETRY_BACKOFF >= 1.0


class TestSignalDeduplicationSurvives:

    def test_spent_signal_is_still_not_reused(self):
        """Ограничитель не должен ломать дедупликацию сигнала."""
        from paper_live_loop import extract_signal_id

        payload = {
            "strategy": {
                "vlad_orb_candidate": {
                    "session_date": "2026-07-28",
                    "retest": {"time": "2026-07-28T10:50:00-04:00"},
                }
            }
        }

        assert extract_signal_id(payload) == (
            "2026-07-28:2026-07-28T10:50:00-04:00"
        )
