import time

import pytest

from api.execution.exchange_adapter import ExchangeAdapter
from api.execution.order_state import OrderStatus, generate_client_order_id
from api.execution.idempotency_store import IdempotencyStore, OrderRecord
from api.execution.order_reconciler import OrderReconciler


class FakeAdapter(ExchangeAdapter):
    """Minimal in-test double — does not hit any network."""

    def __init__(self):
        self.orders = {}
        self.get_order_calls = 0
        self.raise_on_get_order = False

    def place_order(self, client_order_id, order):
        self.orders[client_order_id] = {"status": OrderStatus.ACKNOWLEDGED.value, "order": order}
        return {"client_order_id": client_order_id, "status": "ACKNOWLEDGED"}

    def amend_order(self, client_order_id, changes):
        return {"client_order_id": client_order_id}

    def cancel_order(self, client_order_id):
        return {"client_order_id": client_order_id, "status": "CANCELLED"}

    def get_order(self, client_order_id):
        self.get_order_calls += 1
        if self.raise_on_get_order:
            raise ConnectionError("simulated network failure")
        if client_order_id not in self.orders:
            return {"found": False}
        return {"found": True, "status": self.orders[client_order_id]["status"], "exchange_order_id": "EX-1"}

    def get_open_orders(self, symbol=None):
        return []

    def get_executions(self, client_order_id=None):
        return []

    def get_position(self, symbol):
        return {"symbol": symbol, "size": 0}

    def get_balance(self):
        return {"balance": 1000.0}


@pytest.fixture
def store(tmp_path):
    return IdempotencyStore(state_dir=str(tmp_path / "orders"))


@pytest.fixture
def adapter():
    return FakeAdapter()


class TestClientOrderIdGeneration:

    def test_deterministic_for_identical_inputs(self):
        id1 = generate_client_order_id("bybit", "BTCUSDT", "LONG", 100.0, 98.0, "session-A")
        id2 = generate_client_order_id("bybit", "BTCUSDT", "LONG", 100.0, 98.0, "session-A")
        assert id1 == id2

    def test_different_for_different_inputs(self):
        id1 = generate_client_order_id("bybit", "BTCUSDT", "LONG", 100.0, 98.0, "session-A")
        id2 = generate_client_order_id("bybit", "BTCUSDT", "LONG", 100.0, 97.0, "session-A")
        assert id1 != id2


class TestIdempotencyStore:

    def test_get_or_create_does_not_duplicate(self, store):
        decision = {"symbol": "BTCUSDT"}
        r1 = store.get_or_create("cid-1", decision)
        r2 = store.get_or_create("cid-1", {"symbol": "SHOULD_NOT_OVERWRITE"})

        assert r1 is r2
        assert store.get("cid-1").decision == decision

    def test_restart_recovery_reloads_from_disk(self, tmp_path):
        state_dir = str(tmp_path / "orders")
        store1 = IdempotencyStore(state_dir=state_dir)
        store1.get_or_create("cid-1", {"symbol": "BTCUSDT"})
        store1.update_status("cid-1", OrderStatus.SUBMITTED)

        # Simulate process restart: brand-new store instance, same directory.
        store2 = IdempotencyStore(state_dir=state_dir)

        recovered = store2.get("cid-1")
        assert recovered is not None
        assert recovered.status == OrderStatus.SUBMITTED.value

    def test_corrupted_state_file_does_not_crash_load(self, tmp_path):
        state_dir = tmp_path / "orders"
        state_dir.mkdir(parents=True)
        (state_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

        store = IdempotencyStore(state_dir=str(state_dir))  # must not raise
        assert store.all_records() == []

    def test_pending_or_unknown_lists_only_non_terminal_states(self, store):
        store.get_or_create("cid-1", {})
        store.update_status("cid-1", OrderStatus.SUBMITTED)
        store.get_or_create("cid-2", {})
        store.update_status("cid-2", OrderStatus.CANCELLED)

        pending = {r.client_order_id for r in store.pending_or_unknown()}
        assert pending == {"cid-1"}


class TestOrderReconciler:

    def test_reconciles_acknowledged_order(self, store, adapter):
        decision = {"symbol": "BTCUSDT"}
        store.get_or_create("cid-1", decision)
        adapter.place_order("cid-1", decision)
        store.update_status("cid-1", OrderStatus.SUBMITTED)

        reconciler = OrderReconciler(adapter, store)
        result = reconciler.reconcile_one("cid-1")

        assert result["outcome"] == "reconciled"
        assert store.get("cid-1").status == OrderStatus.ACKNOWLEDGED.value

    def test_unknown_order_before_grace_period_stays_undetermined(self, store, adapter):
        store.get_or_create("cid-1", {})
        store.update_status("cid-1", OrderStatus.SUBMITTED)

        reconciler = OrderReconciler(adapter, store, unknown_grace_period_seconds=3600)
        result = reconciler.reconcile_one("cid-1")

        assert result["outcome"] == "too_early_to_conclude"
        # Status must NOT have been silently escalated to CANCELLED/retry-allowed.
        assert store.get("cid-1").status == OrderStatus.SUBMITTED.value

    def test_unknown_order_after_grace_period_confirmed_never_received(self, store, adapter):
        record = store.get_or_create("cid-1", {})
        store.update_status("cid-1", OrderStatus.SUBMITTED)
        # Force the record to look "old enough" without sleeping in the test.
        record.created_at = time.time() - 3600

        reconciler = OrderReconciler(adapter, store, unknown_grace_period_seconds=1)
        result = reconciler.reconcile_one("cid-1")

        assert result["outcome"] == "confirmed_never_received"
        assert result["retry_allowed"] is True
        assert store.get("cid-1").status == OrderStatus.CANCELLED.value

    def test_reconciliation_query_failure_stays_unknown_never_resends(self, store, adapter):
        """
        CRITICAL invariant: a network failure DURING reconciliation itself
        must never be interpreted as "safe to retry." It must stay UNKNOWN.
        """
        store.get_or_create("cid-1", {})
        store.update_status("cid-1", OrderStatus.SUBMITTED)
        adapter.raise_on_get_order = True

        reconciler = OrderReconciler(adapter, store)
        result = reconciler.reconcile_one("cid-1")

        assert result["outcome"] == "reconciliation_query_failed"
        assert store.get("cid-1").status == OrderStatus.UNKNOWN.value

    def test_reconcile_all_pending_processes_every_non_terminal_record(self, store, adapter):
        decision = {"symbol": "BTCUSDT"}

        store.get_or_create("cid-1", decision)
        adapter.place_order("cid-1", decision)
        store.update_status("cid-1", OrderStatus.SUBMITTED)

        store.get_or_create("cid-2", decision)  # never placed on the exchange
        store.update_status("cid-2", OrderStatus.SUBMITTED)
        store.get("cid-2").created_at = time.time() - 3600

        reconciler = OrderReconciler(adapter, store, unknown_grace_period_seconds=1)
        results = reconciler.reconcile_all_pending()

        outcomes = {r["client_order_id"]: r["outcome"] for r in results}
        assert outcomes["cid-1"] == "reconciled"
        assert outcomes["cid-2"] == "confirmed_never_received"

    def test_no_local_record_is_handled_safely(self, store, adapter):
        reconciler = OrderReconciler(adapter, store)
        result = reconciler.reconcile_one("cid-does-not-exist")
        assert result["outcome"] == "no_local_record"
