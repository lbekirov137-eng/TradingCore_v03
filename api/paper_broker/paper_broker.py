"""
Paper-брокер: реализует ExchangeAdapter полностью в памяти/на диске, без
единого сетевого вызова. Это то, что фактически исполняет "paper trades"
в MVP — детерминированно, воспроизводимо, с комиссией, проскальзыванием
и частичным исполнением.

Модель исполнения (намеренно простая и консервативная, а не
гипер-реалистичная — см. AUTOTRADING_FULL_AUDIT_REPORT.md §8 про то, чего
здесь НЕТ: реального стакана, задержки сети, гэпов):

- MARKET-ордер исполняется немедленно по переданной референсной цене
  с проскальзыванием ПРОТИВ трейдера (BUY исполняется дороже, SELL —
  дешевле) и комиссией, списываемой с баланса.
- LIMIT-ордер остаётся NEW, пока explicitly не проверен против цены
  через check_resting_orders(symbol, price) — так exit-monitor может
  детерминированно "спросить": "цена коснулась SL/TP?" на каждой новой
  свече, не опрашивая реальную биржу.
- Частичное исполнение: если объём ордера превышает
  `liquidity_per_check` (параметр на ордере или брокере), исполняется
  только доступная часть, остаток остаётся открытым как
  PARTIALLY_FILLED — реалистичная, но не точная модель стакана.
- Никакого плеча: BUY ограничен доступным балансом; SHORT не
  поддерживается в этой версии (spot long-only, см. Phase 7 risk engine).

Состояние (баланс, позиции, ордера, журнал) сохраняется на диск после
каждой мутации — процесс может упасть и быть перезапущен без потери
состояния (restart persistence).
"""

import json
import os
import time
from typing import Optional

from api.execution.exchange_adapter import ExchangeAdapter
from api.execution.order_state import OrderStatus

DEFAULT_STATE_PATH = os.path.join("state", "paper_broker.json")


class InsufficientBalanceError(Exception):
    pass


class PaperBroker(ExchangeAdapter):

    def __init__(self, initial_balance: float = 1000.0, fee_rate: float = 0.001,
                 slippage_bps: float = 5.0, state_path: str = DEFAULT_STATE_PATH):

        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.state_path = state_path

        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)

        loaded = self._load()
        if loaded is not None:
            self.balance = loaded["balance"]
            self.positions = loaded["positions"]
            self.orders = loaded["orders"]
            self.ledger = loaded["ledger"]
            self.realized_pnl = loaded["realized_pnl"]
        else:
            self.balance = initial_balance
            self.positions = {}      # symbol -> {"qty", "avg_entry", "direction"}
            self.orders = {}         # client_order_id -> order dict
            self.ledger = []         # append-only audit trail
            self.realized_pnl = 0.0
            self._persist()

    # ---- persistence -------------------------------------------------

    def _persist(self):
        tmp = self.state_path + ".tmp"
        payload = {
            "balance": self.balance,
            "positions": self.positions,
            "orders": self.orders,
            "ledger": self.ledger,
            "realized_pnl": self.realized_pnl,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self.state_path)

    def _load(self) -> Optional[dict]:
        if not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def reset(self, initial_balance: float = 1000.0):
        """Только для тестов."""
        self.balance = initial_balance
        self.positions = {}
        self.orders = {}
        self.ledger = []
        self.realized_pnl = 0.0
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
        self._persist()

    def _log(self, event_type: str, **fields):
        entry = {"event": event_type, "timestamp": time.time(), **fields}
        self.ledger.append(entry)
        return entry

    # ---- ExchangeAdapter interface -----------------------------------

    def place_order(self, client_order_id: str, order: dict) -> dict:
        """
        order = {symbol, side ("BUY"/"SELL"), type ("MARKET"/"LIMIT"),
                 qty, price (reference for MARKET, limit price for LIMIT),
                 liquidity_per_check (optional, default = full qty)}
        """

        existing = self.orders.get(client_order_id)
        if existing is not None:
            # Идемпотентность: тот же client_order_id -> та же запись,
            # ордер не дублируется.
            return {"client_order_id": client_order_id, "status": existing["status"]}

        symbol = order["symbol"]
        side = order["side"]
        order_type = order.get("type", "MARKET")
        qty = order["qty"]
        price = order["price"]
        liquidity_per_check = order.get("liquidity_per_check", qty)

        record = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "qty": qty,
            "price": price,
            "liquidity_per_check": liquidity_per_check,
            "filled_qty": 0.0,
            "avg_fill_price": 0.0,
            "status": OrderStatus.NEW.value,
            "created_at": time.time(),
        }

        self.orders[client_order_id] = record
        self._log("ORDER_PLACED", client_order_id=client_order_id, symbol=symbol, side=side, qty=qty)

        if order_type == "MARKET":
            self._fill(client_order_id, price, qty)
        else:
            record["status"] = OrderStatus.ACKNOWLEDGED.value

        self._persist()

        return {"client_order_id": client_order_id, "status": self.orders[client_order_id]["status"]}

    def amend_order(self, client_order_id: str, changes: dict) -> dict:
        record = self.orders.get(client_order_id)

        if record is None:
            return {"client_order_id": client_order_id, "found": False}

        if record["status"] not in (OrderStatus.NEW.value, OrderStatus.ACKNOWLEDGED.value):
            return {"client_order_id": client_order_id, "amended": False,
                     "reason": f"Cannot amend order in status {record['status']}."}

        for key in ("price", "qty"):
            if key in changes:
                record[key] = changes[key]

        self._log("ORDER_AMENDED", client_order_id=client_order_id, changes=changes)
        self._persist()

        return {"client_order_id": client_order_id, "amended": True}

    def cancel_order(self, client_order_id: str) -> dict:
        record = self.orders.get(client_order_id)

        if record is None:
            return {"client_order_id": client_order_id, "found": False}

        if record["status"] in (OrderStatus.FILLED.value, OrderStatus.CANCELLED.value):
            return {"client_order_id": client_order_id, "status": record["status"], "cancelled": False}

        record["status"] = OrderStatus.CANCELLED.value
        self._log("ORDER_CANCELLED", client_order_id=client_order_id)
        self._persist()

        return {"client_order_id": client_order_id, "status": record["status"], "cancelled": True}

    def get_order(self, client_order_id: str) -> dict:
        record = self.orders.get(client_order_id)

        if record is None:
            return {"found": False}

        return {
            "found": True,
            "status": record["status"],
            "exchange_order_id": client_order_id,  # paper broker: same ID space
            "filled_qty": record["filled_qty"],
            "avg_fill_price": record["avg_fill_price"],
        }

    def get_open_orders(self, symbol: str = None) -> list:
        open_states = {OrderStatus.NEW.value, OrderStatus.ACKNOWLEDGED.value, OrderStatus.PARTIALLY_FILLED.value}
        result = [o for o in self.orders.values() if o["status"] in open_states]
        if symbol is not None:
            result = [o for o in result if o["symbol"] == symbol]
        return result

    def get_executions(self, client_order_id: str = None) -> list:
        fills = [e for e in self.ledger if e["event"] in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED")]
        if client_order_id is not None:
            fills = [e for e in fills if e.get("client_order_id") == client_order_id]
        return fills

    def get_position(self, symbol: str) -> dict:
        position = self.positions.get(symbol)
        if position is None:
            return {"symbol": symbol, "qty": 0.0, "avg_entry": 0.0, "direction": None}
        return {"symbol": symbol, **position}

    def get_balance(self) -> dict:
        locked = self._locked_notional()
        return {
            "balance": round(self.balance, 8),
            "available_balance": round(self.balance - locked, 8),
            "realized_pnl": round(self.realized_pnl, 8),
        }

    def _locked_notional(self) -> float:
        locked = 0.0
        for order in self.orders.values():
            if order["status"] in (OrderStatus.NEW.value, OrderStatus.ACKNOWLEDGED.value):
                remaining = order["qty"] - order["filled_qty"]
                locked += remaining * order["price"]
        return locked

    # ---- fill engine ---------------------------------------------------

    def check_resting_orders(self, symbol: str, high: float, low: float) -> list:
        """
        Проверяет открытые LIMIT-ордера по символу против диапазона
        свечи [low, high] — используется exit-монитором для определения,
        коснулась ли цена SL/TP внутри свечи. Возвращает список
        client_order_id, которые были исполнены (полностью или частично).
        """

        touched = []

        for client_order_id, order in list(self.orders.items()):
            if order["symbol"] != symbol:
                continue
            if order["status"] not in (OrderStatus.NEW.value, OrderStatus.ACKNOWLEDGED.value,
                                        OrderStatus.PARTIALLY_FILLED.value):
                continue

            limit_price = order["price"]

            price_touched = low <= limit_price <= high

            if not price_touched:
                continue

            remaining = order["qty"] - order["filled_qty"]
            fill_qty = min(remaining, order["liquidity_per_check"])

            self._fill(client_order_id, limit_price, fill_qty)
            touched.append(client_order_id)

        if touched:
            self._persist()

        return touched

    def _fill(self, client_order_id: str, reference_price: float, qty: float):

        record = self.orders[client_order_id]
        side = record["side"]
        symbol = record["symbol"]

        slippage = reference_price * (self.slippage_bps / 10_000)
        fill_price = reference_price + slippage if side == "BUY" else reference_price - slippage

        notional = qty * fill_price
        fee = notional * self.fee_rate

        if side == "BUY":
            required = notional + fee
            if required > self.balance + 1e-9:
                # Недостаточно средств — ордер безопасно не исполняется
                # (никогда не уходит в отрицательный баланс/плечо).
                record["status"] = OrderStatus.REJECTED.value
                self._log("ORDER_REJECTED", client_order_id=client_order_id, reason="insufficient_balance")
                return

            self.balance -= required
            self._apply_position_delta(symbol, qty, fill_price, direction="LONG")

        else:  # SELL — closing a long position
            position = self.positions.get(symbol)
            held_qty = position["qty"] if position else 0.0

            sell_qty = min(qty, held_qty)

            if sell_qty <= 0:
                record["status"] = OrderStatus.REJECTED.value
                self._log("ORDER_REJECTED", client_order_id=client_order_id, reason="no_position_to_sell")
                return

            proceeds = sell_qty * fill_price
            self.balance += proceeds - fee

            avg_entry = position["avg_entry"]
            realized = (fill_price - avg_entry) * sell_qty - fee
            self.realized_pnl += realized

            self._apply_position_delta(symbol, -sell_qty, fill_price, direction="LONG")
            qty = sell_qty  # actual filled amount for partial-fill bookkeeping below

        record["filled_qty"] += qty
        record["avg_fill_price"] = fill_price

        if record["filled_qty"] >= record["qty"] - 1e-12:
            record["status"] = OrderStatus.FILLED.value
            event = "ORDER_FILLED"
        else:
            record["status"] = OrderStatus.PARTIALLY_FILLED.value
            event = "ORDER_PARTIALLY_FILLED"

        self._log(event, client_order_id=client_order_id, symbol=symbol, side=side,
                   qty=qty, fill_price=fill_price, fee=fee)

    def _apply_position_delta(self, symbol: str, qty_delta: float, price: float, direction: str):

        position = self.positions.get(symbol)

        if position is None:
            if qty_delta <= 0:
                return
            self.positions[symbol] = {"qty": qty_delta, "avg_entry": price, "direction": direction}
            self._log("POSITION_OPENED", symbol=symbol, qty=qty_delta, entry=price)
            return

        new_qty = position["qty"] + qty_delta

        if qty_delta > 0:
            # Adding to a position: weighted-average entry price.
            total_cost = position["avg_entry"] * position["qty"] + price * qty_delta
            position["avg_entry"] = total_cost / new_qty if new_qty > 0 else price

        position["qty"] = new_qty

        if new_qty <= 1e-12:
            del self.positions[symbol]
            self._log("POSITION_CLOSED", symbol=symbol)
