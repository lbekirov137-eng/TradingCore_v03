from pathlib import Path

import pytest

from api.paper_trading.position_manager import (
    PaperPositionManager,
)


def build_paper_order() -> dict:
    return {
        "mode": "PAPER",
        "status": "FILLED_SIMULATED",
        "real_order_sent": False,
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "side": "BUY",
        "entry": 100.0,
        "quantity": 0.1,
        "stop": 90.0,
        "take_profit_1": 120.0,
        "take_profit_2": 130.0,
        "risk_amount": 1.0,
        "execution_mode": "SPOT_LONG_ONLY",
    }


def build_manager(
    tmp_path: Path,
) -> PaperPositionManager:
    return PaperPositionManager(
        state_file=tmp_path / "paper_position.json"
    )


def test_position_is_opened_and_saved(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    result = manager.open_position(
        build_paper_order(),
        opened_at_utc="2026-07-17T18:00:00+00:00",
    )

    assert result["event"] == "POSITION_OPENED"
    assert result["real_order_sent"] is False

    position = result["position"]

    assert position["status"] == "OPEN"
    assert position["mode"] == "PAPER"
    assert position["side"] == "BUY"
    assert position["entry"] == 100.0
    assert position["quantity"] == 0.1
    assert position["stop"] == 90.0
    assert position["take_profit_1"] == 120.0
    assert position["take_profit_2"] == 130.0
    assert position["real_order_sent"] is False

    assert manager.has_open_position() is True
    assert manager.state_file.exists() is True


def test_second_position_is_not_opened(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.open_position(
        build_paper_order()
    )

    assert result["event"] == "POSITION_ALREADY_OPEN"
    assert result["real_order_sent"] is False
    assert result["position"]["status"] == "OPEN"


def test_position_remains_open(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.evaluate_position(
        market_price=110.0,
        candle_high=115.0,
        candle_low=95.0,
        observed_at_utc="2026-07-17T18:05:00+00:00",
    )

    assert result["event"] == "POSITION_REMAINS_OPEN"
    assert result["real_order_sent"] is False

    position = result["position"]

    assert position["status"] == "OPEN"
    assert position["last_market_price"] == 110.0
    assert position["unrealized_pnl"] == 1.0
    assert position["tp1_reached"] is False


def test_take_profit_1_is_recorded(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.evaluate_position(
        market_price=115.0,
        candle_high=121.0,
        candle_low=100.0,
        observed_at_utc="2026-07-17T18:10:00+00:00",
    )

    assert result["event"] == "TAKE_PROFIT_1_REACHED"
    assert result["real_order_sent"] is False

    position = result["position"]

    assert position["status"] == "OPEN"
    assert position["tp1_reached"] is True
    assert (
        position["tp1_reached_at_utc"]
        == "2026-07-17T18:10:00+00:00"
    )


def test_take_profit_2_closes_position(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.evaluate_position(
        market_price=125.0,
        candle_high=131.0,
        candle_low=100.0,
        observed_at_utc="2026-07-17T18:15:00+00:00",
    )

    assert result["event"] == "POSITION_CLOSED"
    assert result["exit_reason"] == "TAKE_PROFIT_2"
    assert result["exit_price"] == 130.0
    assert result["realized_pnl"] == 3.0
    assert result["real_order_sent"] is False

    position = result["position"]

    assert position["status"] == "CLOSED"
    assert position["real_order_sent"] is False
    assert manager.has_open_position() is False


def test_stop_loss_closes_position(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.evaluate_position(
        market_price=95.0,
        candle_high=105.0,
        candle_low=89.0,
        observed_at_utc="2026-07-17T18:20:00+00:00",
    )

    assert result["event"] == "POSITION_CLOSED"
    assert result["exit_reason"] == "STOP_LOSS"
    assert result["exit_price"] == 90.0
    assert result["realized_pnl"] == -1.0
    assert result["real_order_sent"] is False

    assert result["position"]["status"] == "CLOSED"


def test_stop_has_priority_when_same_candle_hits_both(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.evaluate_position(
        market_price=110.0,
        candle_high=131.0,
        candle_low=89.0,
        observed_at_utc="2026-07-17T18:25:00+00:00",
    )

    assert result["event"] == "POSITION_CLOSED"
    assert result["exit_reason"] == "STOP_LOSS"
    assert result["exit_price"] == 90.0
    assert result["realized_pnl"] == -1.0
    assert result["real_order_sent"] is False


def test_invalid_buy_levels_are_rejected(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    invalid_order = build_paper_order()
    invalid_order["stop"] = 105.0

    with pytest.raises(
        ValueError,
        match="BUY paper order levels must satisfy",
    ):
        manager.open_position(invalid_order)


def test_reset_removes_position_state(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order()
    )

    result = manager.reset_position()

    assert result["event"] == "POSITION_STATE_RESET"
    assert result["real_order_sent"] is False
    assert result["previous_position"]["status"] == "OPEN"

    assert manager.get_position() is None
    assert manager.has_open_position() is False
    assert manager.state_file.exists() is False