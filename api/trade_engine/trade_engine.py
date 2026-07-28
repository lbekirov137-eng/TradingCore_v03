import time

from api.position_manager.position_manager import PositionManager
from api.backtesting.trade_journal import TradeJournal
from api.risk_engine import DailyRiskGuard
from api.risk.guards import (
    LossStreakGuard,
    CooldownAfterLossGuard,
    MaxDrawdownGuard,
    MaxTradesPerSessionGuard,
    DailyLossGuard,
)
from api.execution.order_state import OrderStatus, generate_client_order_id
from api.execution.idempotency_store import IdempotencyStore
from api.execution.order_reconciler import OrderReconciler
from api.paper_broker.paper_broker import PaperBroker

from config.settings import DEFAULT_BALANCE, DEFAULT_FEE_RATE, DEFAULT_SLIPPAGE_BPS

journal = TradeJournal()

# Единый paper-брокер и хранилище идемпотентности процесса. Всё
# исполнение (открытие/закрытие) проходит через них — реального ордера
# на бирже здесь нет и не может быть (см. AUTOTRADING_FULL_AUDIT_REPORT.md §8).
broker = PaperBroker(initial_balance=DEFAULT_BALANCE, fee_rate=DEFAULT_FEE_RATE, slippage_bps=DEFAULT_SLIPPAGE_BPS)
idempotency_store = IdempotencyStore()
reconciler = OrderReconciler(broker, idempotency_store)


class TradeEngine:

    @staticmethod
    def execute(decision):
        """
        Исполняет решение DecisionEngine через paper-брокер (см.
        api/paper_broker/paper_broker.py) с полной идемпотентностью:
        client_order_id детерминирован от решения, поэтому повторный
        вызов execute() с тем же decision (например, после падения
        процесса и рестарта) НИКОГДА не создаёт вторую позицию.

        Реальные ордера НИКОГДА не отправляются — здесь нет ни одного
        вызова, создающего живой ордер на бирже.
        """

        if decision is None or decision.get("decision") != "TRADE":
            return {
                "status": "NO_TRADE",
                "reason": (decision or {}).get("reason", "Нет решения."),
            }

        trade_plan = decision.get("trade_plan") or {}
        signature = decision.get("signature")
        session_key = decision.get("session_key")
        exchange = decision.get("exchange")
        symbol = decision.get("symbol")
        direction = decision.get("direction")
        entry = trade_plan.get("entry")
        stop = trade_plan.get("stop_loss")

        if PositionManager.has_open_position():
            result = {
                "status": "FAILED_SAFELY",
                "reason": "Уже есть открытая позиция. Новая сделка заблокирована.",
            }
            journal.add_trade({**result, "decision": decision})
            return result

        if PositionManager.is_duplicate_signature(signature):
            result = {
                "status": "FAILED_SAFELY",
                "reason": "Повторная отправка идентичного ордера заблокирована.",
            }
            journal.add_trade({**result, "decision": decision})
            return result

        if PositionManager.is_duplicate_session(session_key):
            result = {
                "status": "FAILED_SAFELY",
                "reason": "Эта сессия уже отторгована — повторный вход запрещён.",
            }
            journal.add_trade({**result, "decision": decision})
            return result

        if direction != "LONG":
            # Spot long-only (Phase 7 — нет плеча, нет коротких позиций в этой версии).
            result = {"status": "FAILED_SAFELY", "reason": f"Направление {direction} не поддерживается (spot long-only)."}
            journal.add_trade({**result, "decision": decision})
            return result

        qty = (decision.get("risk") or {}).get("position_size")

        if not qty or qty <= 0:
            result = {"status": "FAILED_SAFELY", "reason": "Некорректный размер позиции — исполнение отменено."}
            journal.add_trade({**result, "decision": decision})
            return result

        client_order_id = generate_client_order_id(exchange, symbol, direction, entry, stop, session_key)

        existing_record = idempotency_store.get_or_create(client_order_id, decision)

        if existing_record.status != OrderStatus.NEW.value:
            # Этот же client_order_id уже обрабатывался ранее (например,
            # повторный вызов после рестарта) — не отправляем повторно,
            # сверяемся с фактическим состоянием у брокера.
            reconcile_result = reconciler.reconcile_one(client_order_id)
            result = {
                "status": "ORDER_PENDING",
                "reason": f"Ордер {client_order_id} уже отслеживается (статус: {existing_record.status}).",
                "client_order_id": client_order_id,
                "reconciliation": reconcile_result,
            }
            journal.add_trade({**result, "decision": decision})
            return result

        idempotency_store.update_status(client_order_id, OrderStatus.SUBMITTED)

        try:
            broker.place_order(client_order_id, {
                "symbol": symbol, "side": "BUY", "type": "MARKET", "qty": qty, "price": entry,
            })
        except Exception as error:
            # Таймаут/сбой при самой отправке -> UNKNOWN, НЕ retry автоматически.
            idempotency_store.update_status(
                client_order_id, OrderStatus.UNKNOWN,
                last_error=f"{type(error).__name__}: {error}",
            )
            result = {
                "status": "ORDER_PENDING",
                "reason": "Ошибка при отправке ордера — статус неизвестен, требуется reconciliation.",
                "client_order_id": client_order_id,
            }
            journal.add_trade({**result, "decision": decision})
            return result

        order_state = broker.get_order(client_order_id)
        exchange_status = order_state.get("status")

        if exchange_status == OrderStatus.REJECTED.value:
            idempotency_store.update_status(client_order_id, OrderStatus.REJECTED)
            result = {
                "status": "FAILED_SAFELY",
                "reason": "Ордер отклонён брокером (недостаточно средств или нет позиции для продажи).",
                "client_order_id": client_order_id,
            }
            journal.add_trade({**result, "decision": decision})
            return result

        if exchange_status != OrderStatus.FILLED.value:
            # ACKNOWLEDGED/PARTIALLY_FILLED — ордер принят, но ещё не полностью
            # исполнен. Позиция считается открытой только после полного филла.
            idempotency_store.update_status(client_order_id, exchange_status)
            result = {
                "status": "ORDER_PENDING",
                "reason": f"Ордер в статусе {exchange_status} — ожидание исполнения.",
                "client_order_id": client_order_id,
            }
            journal.add_trade({**result, "decision": decision})
            return result

        idempotency_store.update_status(client_order_id, OrderStatus.FILLED, exchange_order_id=client_order_id)

        position = {
            "status": "OPEN",
            "exchange": exchange,
            "symbol": symbol,
            "strategy": decision.get("strategy"),
            "direction": direction,
            "entry": order_state.get("avg_fill_price") or entry,
            "stop": stop,
            "take_profit": trade_plan.get("take_profit"),
            "qty": qty,
            "entry_client_order_id": client_order_id,
            "opened_at": time.time(),
            "risk": decision.get("risk"),
        }

        PositionManager.open_position(position, signature=signature, session_key=session_key)

        risk = decision.get("risk") or {}
        DailyRiskGuard.register_trade(risk.get("risk_amount", 0))
        MaxTradesPerSessionGuard.register_trade(session_key)

        result = {**position, "status": "OPENED"}

        journal.add_trade(result)

        return result

    @staticmethod
    def close(reason: str = "manual", exit_price: float = None):
        """Безопасно закрывает открытую paper-позицию через брокер, если она есть."""

        if not PositionManager.has_open_position():
            result = {
                "status": "FAILED_SAFELY",
                "reason": "Нет открытой позиции для закрытия.",
            }
            journal.add_trade(result)
            return result

        position = PositionManager.current_position()
        symbol = position["symbol"]
        qty = position.get("qty")
        close_price = exit_price if exit_price is not None else position.get("entry")

        from api.execution.order_state import generate_exit_client_order_id
        client_order_id = generate_exit_client_order_id(position.get("entry_client_order_id", ""), reason)

        idempotency_store.get_or_create(client_order_id, {"action": "close", "symbol": symbol, "reason": reason})
        idempotency_store.update_status(client_order_id, OrderStatus.SUBMITTED)

        realized_pnl_before = broker.get_balance()["realized_pnl"]

        try:
            broker.place_order(client_order_id, {
                "symbol": symbol, "side": "SELL", "type": "MARKET", "qty": qty, "price": close_price,
            })
        except Exception as error:
            idempotency_store.update_status(client_order_id, OrderStatus.UNKNOWN, last_error=str(error))
            result = {"status": "ORDER_PENDING", "reason": "Ошибка при закрытии — требуется reconciliation."}
            journal.add_trade(result)
            return result

        order_state = broker.get_order(client_order_id)

        if order_state.get("status") != OrderStatus.FILLED.value:
            idempotency_store.update_status(client_order_id, order_state.get("status"))
            result = {
                "status": "PARTIALLY_FILLED" if order_state.get("status") == OrderStatus.PARTIALLY_FILLED.value else "ORDER_PENDING",
                "reason": "Закрытие ещё не полностью исполнено.",
                "filled_qty": order_state.get("filled_qty"),
            }
            journal.add_trade(result)
            return result

        idempotency_store.update_status(client_order_id, OrderStatus.FILLED)

        closed = PositionManager.close_position(reason)

        balance_after = broker.get_balance()
        trade_net_pnl = balance_after["realized_pnl"] - realized_pnl_before

        # Регистрация исхода сделки во всех account-level guard'ах, которые
        # DecisionEngine проверяет ПЕРЕД следующим входом (см.
        # api/decision_engine/decision_engine.py). Это то, что делает
        # LossStreakGuard/CooldownAfterLossGuard/MaxDrawdownGuard/
        # DailyLossGuard реально подключёнными, а не мёртвым кодом.
        LossStreakGuard.register_result(trade_net_pnl)
        CooldownAfterLossGuard.register_result(trade_net_pnl)
        DailyLossGuard.register_result(trade_net_pnl)
        MaxDrawdownGuard.register_equity(balance_after["balance"])

        result = {
            **closed, "status": "CLOSED", "reason": reason,
            "exit_price": order_state.get("avg_fill_price"),
            "net_pnl": round(trade_net_pnl, 8),
        }

        journal.add_trade(result)

        return result

    @staticmethod
    def simulate(signal):
        """Устаревший путь для старого MarketAnalyzer pipeline. Не используется нигде — оставлен как есть."""

        if signal is None:
            return None

        return {
            "status": "OPEN",
            "direction": signal["direction"],
            "entry": signal["entry"],
            "stop": signal["stop"],
            "tp1": signal["tp1"],
            "tp2": signal["tp2"],
            "opened": True,
        }
