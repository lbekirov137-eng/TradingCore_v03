"""
Адаптер официального Bybit Demo Trading окружения.

Реализует тот же ExchangeAdapter, что и PaperBroker, поэтому вся
логика решений/reconciliation/exit-монитора работает без изменений.

Безопасность:
  - подключается ТОЛЬКО к api-demo.bybit.com (проверяется на каждом
    запросе через validate_endpoint);
  - при TRADING_ENVIRONMENT != DEMO любой вызов, требующий подписи,
    отклоняется;
  - секреты читаются из окружения и НИКОГДА не логируются;
  - подпись формируется по схеме Bybit V5 (HMAC-SHA256), сам секрет
    в запрос не попадает;
  - ретрай ТОЛЬКО с тем же orderLinkId (client_order_id) — повтор
    после таймаута не может создать второй ордер, так как Bybit
    отклоняет дублирующийся orderLinkId.

БЕЗ введённых пользователем credentials адаптер не выполняет ни одного
сетевого вызова к приватным эндпоинтам.
"""

import hashlib
import hmac
import json
import os
import time

import requests

from api.execution.exchange_adapter import ExchangeAdapter
from api.execution.order_state import OrderStatus
from api.exchanges.bybit_demo.config import (
    DEMO_REST_URL,
    ConfigurationError,
    ENV_DEMO,
    get_environment,
    validate_endpoint,
    validate_demo_configuration,
)


RECV_WINDOW = "5000"

# Соответствие статусов Bybit -> внутренние статусы.
BYBIT_STATUS_MAP = {
    "New": OrderStatus.ACKNOWLEDGED.value,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED.value,
    "Filled": OrderStatus.FILLED.value,
    "Cancelled": OrderStatus.CANCELLED.value,
    "Rejected": OrderStatus.REJECTED.value,
    "Deactivated": OrderStatus.CANCELLED.value,
    "Triggered": OrderStatus.ACKNOWLEDGED.value,
    "Untriggered": OrderStatus.ACKNOWLEDGED.value,
}


class RateLimitError(Exception):
    pass


class BybitDemoAdapter(ExchangeAdapter):

    def __init__(self, category: str = "spot", session: requests.Session = None,
                 max_retries: int = 3, base_url: str = DEMO_REST_URL):

        self.environment = get_environment()

        if self.environment != ENV_DEMO:
            raise ConfigurationError(
                f"BybitDemoAdapter требует TRADING_ENVIRONMENT=DEMO (текущее: {self.environment})."
            )

        validate_endpoint(base_url, self.environment)

        self.base_url = base_url
        self.category = category
        self.session = session or requests.Session()
        self.max_retries = max_retries

    # ---- credentials / signing --------------------------------------

    def _credentials(self):
        api_key = os.getenv("BYBIT_DEMO_API_KEY")
        api_secret = os.getenv("BYBIT_DEMO_API_SECRET")

        if not api_key or not api_secret:
            raise ConfigurationError(
                "Отсутствуют BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET. "
                "Подключение не выполняется. Значения не выводятся."
            )

        return api_key, api_secret

    def _headers(self, payload: str):
        api_key, api_secret = self._credentials()
        timestamp = str(int(time.time() * 1000))

        to_sign = timestamp + api_key + RECV_WINDOW + payload

        signature = hmac.new(
            api_secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256,
        ).hexdigest()

        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
        }

    # ---- transport ---------------------------------------------------

    def _request(self, method: str, path: str, params: dict = None, signed: bool = True):

        url = self.base_url + path
        validate_endpoint(url, self.environment)

        params = params or {}

        for attempt in range(self.max_retries):
            try:
                if method == "GET":
                    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                    headers = self._headers(query) if signed else {}
                    response = self.session.get(url, params=params, headers=headers, timeout=15)
                else:
                    body = json.dumps(params, separators=(",", ":"))
                    headers = self._headers(body) if signed else {"Content-Type": "application/json"}
                    response = self.session.post(url, data=body, headers=headers, timeout=15)

                if response.status_code == 429:
                    # Rate limit — экспоненциальная пауза, тот же orderLinkId
                    # гарантирует, что повтор не создаст дубликат.
                    wait = 2 ** attempt
                    if attempt == self.max_retries - 1:
                        raise RateLimitError("Bybit rate limit — превышено число попыток.")
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                payload = response.json()

                ret_code = payload.get("retCode")

                if ret_code not in (0, None):
                    return {"ok": False, "retCode": ret_code, "retMsg": payload.get("retMsg"), "raw": payload}

                return {"ok": True, "result": payload.get("result", {}), "raw": payload}

            except (requests.Timeout, requests.ConnectionError) as error:
                if attempt == self.max_retries - 1:
                    # Важно: таймаут НЕ означает "ордер не создан".
                    # Вызывающий код обязан провести reconciliation.
                    raise TimeoutError(f"Bybit demo request failed after retries: {error}")
                time.sleep(2 ** attempt)

        raise TimeoutError("Bybit demo request exhausted retries.")

    # ---- ExchangeAdapter --------------------------------------------

    def place_order(self, client_order_id: str, order: dict) -> dict:
        """
        orderLinkId = client_order_id обеспечивает идемпотентность на
        стороне Bybit: повтор с тем же ID не создаёт второй ордер.
        """

        params = {
            "category": self.category,
            "symbol": order["symbol"],
            "side": "Buy" if order["side"].upper() == "BUY" else "Sell",
            "orderType": "Market" if order.get("type", "MARKET").upper() == "MARKET" else "Limit",
            "qty": str(order["qty"]),
            "orderLinkId": client_order_id,
        }

        if params["orderType"] == "Limit":
            params["price"] = str(order["price"])

        result = self._request("POST", "/v5/order/create", params)

        if not result["ok"]:
            # Дублирующийся orderLinkId — это НЕ ошибка исполнения:
            # значит ордер уже принят ранее.
            if str(result.get("retCode")) in ("10005", "110072"):
                return {"client_order_id": client_order_id, "status": OrderStatus.ACKNOWLEDGED.value,
                        "duplicate": True}
            return {"client_order_id": client_order_id, "status": OrderStatus.REJECTED.value,
                    "reason": result.get("retMsg")}

        return {
            "client_order_id": client_order_id,
            "status": OrderStatus.ACKNOWLEDGED.value,
            "exchange_order_id": result["result"].get("orderId"),
        }

    def amend_order(self, client_order_id: str, changes: dict) -> dict:
        params = {"category": self.category, "orderLinkId": client_order_id}

        if "price" in changes:
            params["price"] = str(changes["price"])
        if "qty" in changes:
            params["qty"] = str(changes["qty"])
        if "symbol" in changes:
            params["symbol"] = changes["symbol"]

        result = self._request("POST", "/v5/order/amend", params)

        return {"client_order_id": client_order_id, "amended": result["ok"],
                "reason": None if result["ok"] else result.get("retMsg")}

    def cancel_order(self, client_order_id: str, symbol: str = None) -> dict:
        params = {"category": self.category, "orderLinkId": client_order_id}
        if symbol:
            params["symbol"] = symbol

        result = self._request("POST", "/v5/order/cancel", params)

        return {"client_order_id": client_order_id, "cancelled": result["ok"],
                "reason": None if result["ok"] else result.get("retMsg")}

    def get_order(self, client_order_id: str, symbol: str = None) -> dict:
        params = {"category": self.category, "orderLinkId": client_order_id}
        if symbol:
            params["symbol"] = symbol

        result = self._request("GET", "/v5/order/realtime", params)

        if not result["ok"]:
            return {"found": False, "reason": result.get("retMsg")}

        orders = result["result"].get("list") or []

        if not orders:
            history = self._request("GET", "/v5/order/history", params)
            if history["ok"]:
                orders = history["result"].get("list") or []

        if not orders:
            return {"found": False}

        order = orders[0]

        return {
            "found": True,
            "status": BYBIT_STATUS_MAP.get(order.get("orderStatus"), OrderStatus.UNKNOWN.value),
            "exchange_order_id": order.get("orderId"),
            "filled_qty": float(order.get("cumExecQty") or 0),
            "avg_fill_price": float(order.get("avgPrice") or 0) if order.get("avgPrice") else 0.0,
        }

    def get_open_orders(self, symbol: str = None) -> list:
        params = {"category": self.category}
        if symbol:
            params["symbol"] = symbol

        result = self._request("GET", "/v5/order/realtime", params)

        if not result["ok"]:
            return []

        return result["result"].get("list") or []

    def get_executions(self, client_order_id: str = None, symbol: str = None) -> list:
        params = {"category": self.category}
        if client_order_id:
            params["orderLinkId"] = client_order_id
        if symbol:
            params["symbol"] = symbol

        result = self._request("GET", "/v5/execution/list", params)

        if not result["ok"]:
            return []

        return result["result"].get("list") or []

    def get_position(self, symbol: str) -> dict:
        """
        Для spot-категории Bybit позиции выражаются через баланс монеты.
        Возвращается унифицированная структура, совместимая с PaperBroker.
        """

        if self.category == "spot":
            balance = self.get_balance()
            base_asset = symbol.replace("USDT", "")
            qty = balance.get("coins", {}).get(base_asset, 0.0)
            return {"symbol": symbol, "qty": qty, "avg_entry": 0.0,
                    "direction": "LONG" if qty > 0 else None}

        result = self._request("GET", "/v5/position/list",
                                {"category": self.category, "symbol": symbol})

        if not result["ok"]:
            return {"symbol": symbol, "qty": 0.0, "avg_entry": 0.0, "direction": None}

        positions = result["result"].get("list") or []

        if not positions:
            return {"symbol": symbol, "qty": 0.0, "avg_entry": 0.0, "direction": None}

        position = positions[0]
        size = float(position.get("size") or 0)

        return {
            "symbol": symbol,
            "qty": size,
            "avg_entry": float(position.get("avgPrice") or 0),
            "direction": "LONG" if position.get("side") == "Buy" else ("SHORT" if size else None),
        }

    def get_balance(self) -> dict:
        result = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})

        if not result["ok"]:
            return {"balance": 0.0, "available_balance": 0.0, "coins": {}}

        accounts = result["result"].get("list") or []

        if not accounts:
            return {"balance": 0.0, "available_balance": 0.0, "coins": {}}

        account = accounts[0]

        coins = {}
        for coin in account.get("coin", []):
            try:
                coins[coin["coin"]] = float(coin.get("walletBalance") or 0)
            except (TypeError, ValueError):
                continue

        return {
            "balance": float(account.get("totalEquity") or 0),
            "available_balance": float(account.get("totalAvailableBalance") or 0),
            "coins": coins,
        }

    # ---- market data (public, unsigned) ------------------------------

    def get_klines(self, symbol: str, interval: str = "5", limit: int = 200) -> dict:
        result = self._request("GET", "/v5/market/kline", {
            "category": self.category, "symbol": symbol,
            "interval": interval, "limit": limit,
        }, signed=False)

        if not result["ok"]:
            return {}

        rows = result["result"].get("list") or []
        rows = list(reversed(rows))  # Bybit returns newest-first

        return {
            "timestamps": [int(r[0]) for r in rows],
            "opens": [float(r[1]) for r in rows],
            "highs": [float(r[2]) for r in rows],
            "lows": [float(r[3]) for r in rows],
            "closes": [float(r[4]) for r in rows],
            "volumes": [float(r[5]) for r in rows],
        }

    # ---- operator helpers --------------------------------------------

    @staticmethod
    def preflight() -> dict:
        """Проверка конфигурации без подключения и без вывода секретов."""
        return validate_demo_configuration()
