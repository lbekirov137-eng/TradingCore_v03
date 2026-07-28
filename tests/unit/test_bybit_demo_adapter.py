"""
Мокированные тесты Bybit Demo адаптера. Ни один тест НЕ выполняет
реальных сетевых вызовов и не требует настоящих credentials.
"""

import json

import pytest

from api.exchanges.bybit_demo.config import (
    ConfigurationError,
    DEMO_REST_URL,
    validate_endpoint,
    validate_demo_configuration,
    credentials_present,
)
from api.exchanges.bybit_demo.adapter import BybitDemoAdapter, BYBIT_STATUS_MAP
from api.execution.order_state import OrderStatus


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("TRADING_ENVIRONMENT", "DEMO")
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "test-key-not-real")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "test-secret-not-real")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def _respond(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        for fragment, payload in self.responses.items():
            if fragment in url:
                if callable(payload):
                    return payload(self.calls)
                return FakeResponse(payload)
        return FakeResponse({"retCode": 0, "result": {}})

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


class TestEndpointSafety:

    def test_production_endpoint_rejected_in_demo_mode(self):
        with pytest.raises(ConfigurationError):
            validate_endpoint("https://api.bybit.com/v5/order/create", "DEMO")

    def test_bytick_production_mirror_rejected_in_demo(self):
        with pytest.raises(ConfigurationError):
            validate_endpoint("https://api.bytick.com/v5/order/create", "DEMO")

    def test_demo_endpoint_accepted(self):
        validate_endpoint(f"{DEMO_REST_URL}/v5/order/create", "DEMO")  # must not raise

    def test_arbitrary_host_rejected_in_demo(self):
        with pytest.raises(ConfigurationError):
            validate_endpoint("https://evil.example.com/v5/order/create", "DEMO")

    def test_adapter_refuses_to_construct_outside_demo_mode(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "PAPER")
        with pytest.raises(ConfigurationError):
            BybitDemoAdapter()

    def test_adapter_refuses_live_environment(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "LIVE")
        with pytest.raises(ConfigurationError):
            BybitDemoAdapter()

    def test_adapter_refuses_production_base_url_even_in_demo(self, demo_env):
        with pytest.raises(ConfigurationError):
            BybitDemoAdapter(base_url="https://api.bybit.com")


class TestCredentialHandling:

    def test_validate_reports_missing_credentials_without_values(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "DEMO")
        monkeypatch.delenv("BYBIT_DEMO_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_DEMO_API_SECRET", raising=False)

        report = validate_demo_configuration()

        assert report["ready"] is False
        assert "BYBIT_DEMO_API_KEY" in report["reason"]
        # Only booleans are exposed — never the values themselves.
        assert set(report["credentials"].values()) <= {True, False}

    def test_credentials_present_never_returns_values(self, demo_env):
        report = credentials_present()
        for value in report.values():
            assert isinstance(value, bool)

    def test_live_environment_raises_in_validation(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "LIVE")
        with pytest.raises(ConfigurationError):
            validate_demo_configuration()

    def test_signing_fails_safely_without_credentials(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "DEMO")
        monkeypatch.setenv("BYBIT_DEMO_API_KEY", "k")
        monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "s")
        adapter = BybitDemoAdapter(session=FakeSession())

        monkeypatch.delenv("BYBIT_DEMO_API_KEY")

        with pytest.raises(ConfigurationError):
            adapter._credentials()

    def test_secret_never_appears_in_headers(self, demo_env):
        adapter = BybitDemoAdapter(session=FakeSession())
        headers = adapter._headers("test-payload")

        serialized = json.dumps(headers)
        assert "test-secret-not-real" not in serialized
        assert "X-BAPI-SIGN" in headers


class TestOrderOperations:

    def test_place_order_uses_client_order_id_as_orderlinkid(self, demo_env):
        session = FakeSession({"/v5/order/create": {"retCode": 0, "result": {"orderId": "EX-1"}}})
        adapter = BybitDemoAdapter(session=session)

        result = adapter.place_order("tc-abc123", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 0.01, "price": 100.0,
        })

        assert result["status"] == OrderStatus.ACKNOWLEDGED.value
        body = json.loads(session.calls[0]["data"])
        assert body["orderLinkId"] == "tc-abc123"

    def test_duplicate_order_link_id_is_not_treated_as_failure(self, demo_env):
        session = FakeSession({"/v5/order/create": {"retCode": 110072, "retMsg": "duplicate orderLinkId"}})
        adapter = BybitDemoAdapter(session=session)

        result = adapter.place_order("tc-dup", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 0.01, "price": 100.0,
        })

        assert result["duplicate"] is True
        assert result["status"] == OrderStatus.ACKNOWLEDGED.value

    def test_rejected_order_reports_reason(self, demo_env):
        session = FakeSession({"/v5/order/create": {"retCode": 170131, "retMsg": "Insufficient balance"}})
        adapter = BybitDemoAdapter(session=session)

        result = adapter.place_order("tc-rej", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 999.0, "price": 100.0,
        })

        assert result["status"] == OrderStatus.REJECTED.value
        assert "Insufficient" in result["reason"]

    def test_get_order_maps_bybit_status(self, demo_env):
        session = FakeSession({"/v5/order/realtime": {
            "retCode": 0,
            "result": {"list": [{
                "orderStatus": "Filled", "orderId": "EX-9",
                "cumExecQty": "0.01", "avgPrice": "100.5",
            }]},
        }})
        adapter = BybitDemoAdapter(session=session)

        state = adapter.get_order("tc-1")

        assert state["found"] is True
        assert state["status"] == OrderStatus.FILLED.value
        assert state["filled_qty"] == 0.01

    def test_get_order_not_found_returns_found_false(self, demo_env):
        session = FakeSession({
            "/v5/order/realtime": {"retCode": 0, "result": {"list": []}},
            "/v5/order/history": {"retCode": 0, "result": {"list": []}},
        })
        adapter = BybitDemoAdapter(session=session)

        state = adapter.get_order("tc-missing")
        assert state["found"] is False

    def test_partially_filled_status_is_mapped(self, demo_env):
        assert BYBIT_STATUS_MAP["PartiallyFilled"] == OrderStatus.PARTIALLY_FILLED.value


class TestResilience:

    def test_rate_limit_retries_then_raises(self, demo_env, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)

        session = FakeSession({"/v5/order/create": lambda calls: FakeResponse({}, status_code=429)})
        adapter = BybitDemoAdapter(session=session, max_retries=3)

        from api.exchanges.bybit_demo.adapter import RateLimitError

        with pytest.raises(RateLimitError):
            adapter.place_order("tc-rl", {
                "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 0.01, "price": 100.0,
            })

        assert len(session.calls) == 3

    def test_rate_limit_recovers_on_retry_with_same_order_link_id(self, demo_env, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)

        def responder(calls):
            if len(calls) < 2:
                return FakeResponse({}, status_code=429)
            return FakeResponse({"retCode": 0, "result": {"orderId": "EX-2"}})

        session = FakeSession({"/v5/order/create": responder})
        adapter = BybitDemoAdapter(session=session, max_retries=3)

        result = adapter.place_order("tc-same-id", {
            "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 0.01, "price": 100.0,
        })

        assert result["status"] == OrderStatus.ACKNOWLEDGED.value
        # CRITICAL: every retry must reuse the identical orderLinkId,
        # otherwise a retry could create a second real order.
        link_ids = {json.loads(c["data"])["orderLinkId"] for c in session.calls}
        assert link_ids == {"tc-same-id"}

    def test_timeout_raises_and_never_silently_resends(self, demo_env, monkeypatch):
        import requests as rq
        monkeypatch.setattr("time.sleep", lambda *_: None)

        class TimingOutSession:
            def __init__(self):
                self.calls = 0

            def post(self, *a, **kw):
                self.calls += 1
                raise rq.Timeout("simulated timeout")

            def get(self, *a, **kw):
                self.calls += 1
                raise rq.Timeout("simulated timeout")

        session = TimingOutSession()
        adapter = BybitDemoAdapter(session=session, max_retries=2)

        with pytest.raises(TimeoutError):
            adapter.place_order("tc-timeout", {
                "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "qty": 0.01, "price": 100.0,
            })


class TestPreflight:

    def test_preflight_reports_not_ready_without_credentials(self, monkeypatch):
        monkeypatch.setenv("TRADING_ENVIRONMENT", "DEMO")
        monkeypatch.delenv("BYBIT_DEMO_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_DEMO_API_SECRET", raising=False)

        report = BybitDemoAdapter.preflight()
        assert report["ready"] is False

    def test_preflight_ready_with_credentials(self, demo_env):
        report = BybitDemoAdapter.preflight()
        assert report["ready"] is True
        assert report["rest_url"] == DEMO_REST_URL
