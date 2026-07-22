from pathlib import Path
from types import SimpleNamespace

from api.paper_trading.position_manager import (
    PaperPositionManager,
)
from paper_live_loop import process_closed_candle


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


def build_pipeline_result(
    paper_order: dict | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        strategy={
            "signal": (
                "BUY"
                if paper_order is not None
                else "NO TRADE"
            )
        },
        risk={
            "allowed": paper_order is not None,
        },
        decision={
            "decision": (
                "TRADE"
                if paper_order is not None
                else "NO_TRADE"
            )
        },
        execution={
            "trade_plan": {},
            "paper_order": (
                paper_order
                if paper_order is not None
                else {
                    "mode": "PAPER",
                    "status": "SKIPPED",
                    "real_order_sent": False,
                    "reason": "No approved trade",
                }
            ),
        },
    )


class TradeEngine:

    def execute(self, context):
        return build_pipeline_result(
            build_paper_order()
        )


class NoTradeEngine:

    def execute(self, context):
        return build_pipeline_result(None)


class ForbiddenEngine:

    def execute(self, context):
        raise AssertionError(
            "Pipeline must not run while "
            "a paper position is open"
        )


def build_manager(
    tmp_path: Path,
) -> PaperPositionManager:
    return PaperPositionManager(
        state_file=tmp_path / "paper_position.json"
    )


def build_snapshot(
    price: float,
    high: float,
    low: float,
) -> dict:
    return {
        "close_time_ms": 1_000,
        "price": price,
        "candle_high": high,
        "candle_low": low,
    }


def test_pipeline_trade_opens_position(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    class SafeTradeEngine(TradeEngine):
        def execute(self, context):
            result = super().execute(context)
            result.execution["paper_order"]["stop"] = 96.0
            return result


    position_event, pipeline_data = (
        process_closed_candle(
            engine=SafeTradeEngine(),
            position_manager=manager,
            context=object(),
            snapshot=build_snapshot(
                price=100.0,
                high=105.0,
                low=95.0,
            ),
        )
    )

    assert (
        position_event["event"]
        == "POSITION_OPENED"
    )
    assert pipeline_data is not None

    assert (
        pipeline_data["decision"]["decision"]
        == "TRADE"
    )

    assert manager.has_open_position() is True

    position = manager.get_position()

    assert position is not None
    assert position["status"] == "OPEN"
    assert position["entry"] == 100.0
    assert position["real_order_sent"] is False


def test_open_position_is_managed_without_new_pipeline(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order(),
        opened_at_utc="2026-07-17T18:00:00+00:00",
    )

    position_event, pipeline_data = (
        process_closed_candle(
            engine=ForbiddenEngine(),
            position_manager=manager,
            context=object(),
            snapshot=build_snapshot(
                price=110.0,
                high=115.0,
                low=95.0,
            ),
        )
    )

    assert (
        position_event["event"]
        == "POSITION_REMAINS_OPEN"
    )

    assert pipeline_data is None
    assert manager.has_open_position() is True

    position = manager.get_position()

    assert position is not None
    assert position["last_market_price"] == 110.0
    assert position["real_order_sent"] is False


def test_stop_closes_existing_position_without_reentry(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    manager.open_position(
        build_paper_order(),
        opened_at_utc="2026-07-17T18:00:00+00:00",
    )

    position_event, pipeline_data = (
        process_closed_candle(
            engine=ForbiddenEngine(),
            position_manager=manager,
            context=object(),
            snapshot=build_snapshot(
                price=95.0,
                high=105.0,
                low=89.0,
            ),
        )
    )

    assert (
        position_event["event"]
        == "POSITION_CLOSED"
    )

    assert (
        position_event["exit_reason"]
        == "STOP_LOSS"
    )

    assert position_event["exit_price"] == 90.0
    assert position_event["realized_pnl"] == -1.0
    assert position_event["real_order_sent"] is False

    assert pipeline_data is None
    assert manager.has_open_position() is False


def test_no_trade_does_not_open_position(
    tmp_path: Path,
) -> None:
    manager = build_manager(tmp_path)

    position_event, pipeline_data = (
        process_closed_candle(
            engine=NoTradeEngine(),
            position_manager=manager,
            context=object(),
            snapshot=build_snapshot(
                price=100.0,
                high=105.0,
                low=95.0,
            ),
        )
    )

    assert (
        position_event["event"]
        == "NO_POSITION_OPENED"
    )

    assert pipeline_data is not None

    assert (
        pipeline_data["decision"]["decision"]
        == "NO_TRADE"
    )

    assert manager.has_open_position() is False
    assert position_event["real_order_sent"] is False