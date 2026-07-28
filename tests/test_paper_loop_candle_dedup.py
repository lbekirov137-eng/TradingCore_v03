"""
Проверка дедупликации свечей в paper-цикле.

Инвариант: пока биржа отдаёт ту же самую закрытую свечу
(один и тот же close_time_ms), она обрабатывается РОВНО ОДИН раз.
Опрос идёт каждые POLL_INTERVAL_SECONDS, а свеча 5m закрывается раз в
5 минут, поэтому без дедупликации одна свеча обрабатывалась бы ~10 раз
подряд, засоряя журнал и повторно открывая позиции.

Здесь же фиксируется, что появление НОВОЙ свечи снимает блокировку,
то есть дедупликация не превращается в залипание.
"""

import threading

import pytest


class DummyContext:
    exchange = "binance"
    symbol = "BTCUSDT"
    timeframe = "5m"
    market = {"source": "TEST"}


def make_snapshot(close_time_ms: int) -> dict:
    return {
        "close_time_ms": close_time_ms,
        "price": 100.0,
        "candle_high": 101.0,
        "candle_low": 99.0,
    }


@pytest.fixture
def loop_module(monkeypatch, tmp_path):
    """Изолированный цикл: без сети, без Telegram, без записи в репозиторий."""
    import paper_live_loop as loop

    monkeypatch.setattr(
        loop, "POSITION_STATE_FILE", tmp_path / "pos.json"
    )
    monkeypatch.setattr(loop, "POLL_INTERVAL_SECONDS", 0.01)

    monkeypatch.setattr(loop, "build_live_context", lambda: DummyContext())
    monkeypatch.setattr(loop, "append_journal", lambda record: None)
    monkeypatch.setattr(loop, "save_runtime_state", lambda value: None)
    monkeypatch.setattr(loop, "load_runtime_state", lambda: {})
    monkeypatch.setattr(loop, "print_result", lambda record: None)
    monkeypatch.setattr(loop, "log_decision_line", lambda record: None)
    monkeypatch.setattr(
        loop, "send_event_notification", lambda record: False
    )
    monkeypatch.setattr(
        loop,
        "build_daily_opportunity_summary",
        lambda date_utc: "",
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

    return loop


def run_loop_briefly(loop, iterations_window: float = 0.6) -> None:
    """Крутит цикл в потоке и гарантированно останавливает его."""
    stop_event = threading.Event()

    thread = threading.Thread(
        target=loop.main,
        kwargs={"stop_event": stop_event},
        daemon=True,
    )
    thread.start()

    # Окно заведомо больше многих периодов опроса (0.01 c).
    stop_event.wait(iterations_window)
    stop_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive(), "цикл не остановился по stop_event"


def test_same_candle_is_processed_only_once(loop_module, monkeypatch):
    processed = []

    monkeypatch.setattr(
        loop_module,
        "extract_market_snapshot",
        lambda context: make_snapshot(1_700_000_000_000),
    )

    def fake_process(**kwargs):
        processed.append(kwargs["snapshot"]["close_time_ms"])
        return ({"event": "NO_TRADE", "position": None}, {})

    monkeypatch.setattr(
        loop_module, "process_closed_candle", fake_process
    )

    run_loop_briefly(loop_module)

    # За окно 0.6 c при опросе 0.01 c было ~60 итераций опроса,
    # но обработка обязана произойти ровно один раз.
    assert processed == [1_700_000_000_000], (
        f"свеча обработана {len(processed)} раз(а) вместо одного"
    )


def test_new_candle_is_processed_and_dedup_does_not_stick(
    loop_module, monkeypatch
):
    processed = []

    # Первая свеча, затем навсегда вторая.
    sequence = [1_700_000_000_000, 1_700_000_300_000]
    state = {"index": 0}

    def advancing_snapshot(context):
        index = min(state["index"], len(sequence) - 1)
        snapshot = make_snapshot(sequence[index])
        state["index"] += 1
        return snapshot

    monkeypatch.setattr(
        loop_module, "extract_market_snapshot", advancing_snapshot
    )

    def fake_process(**kwargs):
        processed.append(kwargs["snapshot"]["close_time_ms"])
        return ({"event": "NO_TRADE", "position": None}, {})

    monkeypatch.setattr(
        loop_module, "process_closed_candle", fake_process
    )

    run_loop_briefly(loop_module)

    assert processed == sequence, (
        f"ожидалась обработка обеих свечей по разу, получено: {processed}"
    )
