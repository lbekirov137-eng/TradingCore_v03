import json
import os

import pytest

from api.paper_broker.paper_broker import PaperBroker
from api.execution.order_state import OrderStatus


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(initial_balance=1000.0, fee_rate=0.001, slippage_bps=5.0,
                        state_path=str(tmp_path / "broker.json"))


class TestMarketOrderFill:

    def test_market_buy_fills_immediately_with_slippage_and_fee(self, broker):
        result = broker.place_order("cid-1", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0,
        })

        assert result["status"] == OrderStatus.FILLED.value

        expected_slippage = 100.0 * (5.0 / 10_000)
        expected_fill_price = 100.0 + expected_slippage
        expected_fee = 1.0 * expected_fill_price * 0.001
        expected_balance = 1000.0 - (1.0 * expected_fill_price + expected_fee)

        assert broker.balance == pytest.approx(expected_balance)

        position = broker.get_position("BTCUSDT")
        assert position["qty"] == pytest.approx(1.0)
        assert position["avg_entry"] == pytest.approx(expected_fill_price)

    def test_market_sell_realizes_pnl(self, broker):
        broker.place_order("cid-buy", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})
        broker.place_order("cid-sell", {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "qty": 1.0, "price": 110.0})

        balance = broker.get_balance()
        assert balance["realized_pnl"] > 0  # bought at ~100, sold at ~110 -> profit

        position = broker.get_position("BTCUSDT")
        assert position["qty"] == 0.0

    def test_buy_rejected_when_insufficient_balance_never_goes_negative(self, broker):
        result = broker.place_order("cid-1", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 100.0, "price": 100.0,
        })  # notional=10,000 >> balance=1000

        status = broker.get_order("cid-1")
        assert status["status"] == OrderStatus.REJECTED.value
        assert broker.balance == 1000.0  # untouched
        assert broker.get_position("BTCUSDT")["qty"] == 0.0

    def test_sell_without_position_rejected_not_crashed(self, broker):
        broker.place_order("cid-1", {
            "symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "qty": 1.0, "price": 100.0,
        })
        status = broker.get_order("cid-1")
        assert status["status"] == OrderStatus.REJECTED.value


class TestIdempotency:

    def test_placing_same_client_order_id_twice_does_not_double_fill(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})
        balance_after_first = broker.balance

        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})

        assert broker.balance == balance_after_first
        assert broker.get_position("BTCUSDT")["qty"] == pytest.approx(1.0)


class TestLimitOrdersAndPartialFills:

    def test_limit_order_stays_open_until_price_touched(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 1.0, "price": 95.0})

        status = broker.get_order("cid-1")
        assert status["status"] == OrderStatus.ACKNOWLEDGED.value

        touched = broker.check_resting_orders("BTCUSDT", high=99.0, low=97.0)  # doesn't reach 95
        assert touched == []
        assert broker.get_order("cid-1")["status"] == OrderStatus.ACKNOWLEDGED.value

    def test_limit_order_fills_when_price_touches(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 1.0, "price": 95.0})

        touched = broker.check_resting_orders("BTCUSDT", high=99.0, low=94.0)  # range includes 95
        assert touched == ["cid-1"]
        assert broker.get_order("cid-1")["status"] == OrderStatus.FILLED.value

    def test_partial_fill_when_liquidity_limited(self, broker):
        broker.place_order("cid-1", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 10.0, "price": 95.0,
            "liquidity_per_check": 4.0,
        })

        broker.check_resting_orders("BTCUSDT", high=99.0, low=94.0)
        status = broker.get_order("cid-1")
        assert status["status"] == OrderStatus.PARTIALLY_FILLED.value
        assert status["filled_qty"] == pytest.approx(4.0)

        # A second candle touching the price fills more of the remainder.
        broker.check_resting_orders("BTCUSDT", high=99.0, low=94.0)
        status = broker.get_order("cid-1")
        assert status["filled_qty"] == pytest.approx(8.0)
        assert status["status"] == OrderStatus.PARTIALLY_FILLED.value

        broker.check_resting_orders("BTCUSDT", high=99.0, low=94.0)
        status = broker.get_order("cid-1")
        assert status["status"] == OrderStatus.FILLED.value
        assert status["filled_qty"] == pytest.approx(10.0)


class TestCancelAndAmend:

    def test_cancel_open_order(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 1.0, "price": 95.0})
        result = broker.cancel_order("cid-1")
        assert result["cancelled"] is True
        assert broker.get_order("cid-1")["status"] == OrderStatus.CANCELLED.value

    def test_cannot_cancel_already_filled_order(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})
        result = broker.cancel_order("cid-1")
        assert result["cancelled"] is False

    def test_amend_open_limit_order_price(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 1.0, "price": 95.0})
        result = broker.amend_order("cid-1", {"price": 93.0})
        assert result["amended"] is True
        assert broker.orders["cid-1"]["price"] == 93.0

    def test_cannot_amend_filled_order(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})
        result = broker.amend_order("cid-1", {"price": 93.0})
        assert result["amended"] is False


class TestBalanceAndAvailability:

    def test_open_limit_order_locks_notional_from_available_balance(self, broker):
        broker.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 1.0, "price": 100.0})

        balance = broker.get_balance()
        assert balance["balance"] == 1000.0
        assert balance["available_balance"] == pytest.approx(900.0)


class TestRestartPersistence:

    def test_state_survives_new_instance_same_path(self, tmp_path):
        path = str(tmp_path / "broker.json")
        broker1 = PaperBroker(initial_balance=1000.0, state_path=path)
        broker1.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})

        broker2 = PaperBroker(initial_balance=1000.0, state_path=path)  # simulated restart

        assert broker2.get_position("BTCUSDT")["qty"] == pytest.approx(1.0)
        assert broker2.get_order("cid-1")["status"] == OrderStatus.FILLED.value
        assert broker2.balance == broker1.balance

    def test_corrupted_state_file_falls_back_to_fresh_state_not_crash(self, tmp_path):
        path = tmp_path / "broker.json"
        path.write_text("{not valid json", encoding="utf-8")

        broker = PaperBroker(initial_balance=1000.0, state_path=str(path))  # must not raise
        assert broker.balance == 1000.0


class TestDeterministicReplay:

    def test_same_sequence_of_orders_produces_same_final_state(self, tmp_path):
        def run():
            b = PaperBroker(initial_balance=1000.0, state_path=str(tmp_path / f"replay-{id(object())}.json"))
            b.place_order("cid-1", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 1.0, "price": 100.0})
            b.place_order("cid-2", {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "qty": 1.0, "price": 105.0})
            return b.balance, b.realized_pnl

        result_a = run()
        result_b = run()

        assert result_a == result_b
