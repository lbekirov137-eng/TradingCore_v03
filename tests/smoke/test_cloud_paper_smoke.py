"""
Детерминированный smoke test cloud paper monitor конфигурации.

Запуск отдельно перед любым облачным деплоем:
    .venv/Scripts/python.exe -m pytest tests/smoke -v

Каждый тест соответствует одному пункту обязательного чек-листа:
  1. приложение запускается;
  2. health endpoint отвечает;
  3. live trading выключен;
  4. market-data failure обрабатывается безопасно;
  5. один цикл monitor выполняется;
  6. виртуальный ордер не отправляется на биржу;
  7. restart не дублирует решение.
"""

import pytest
from fastapi.testclient import TestClient

from api.data_engine import DataEngine
from api.scheduler.loop import SchedulerLoop
from api.trade_engine import trade_engine as te
from api.execution.order_reconciler import OrderReconciler
from api.position_manager.position_manager import PositionManager

from tests.conftest import orb_breakout_snapshot


def test_1_application_starts_without_raising():
    """Импорт приложения (эквивалент запуска процесса) не бросает исключение."""
    from api.server import app
    assert app is not None


def test_2_health_endpoint_responds():
    from api.server import app
    client = TestClient(app)

    r = client.get("/health")

    assert r.status_code == 200
    assert "status" in r.json()


def test_3_live_trading_is_off_by_default():
    from api.server import app
    client = TestClient(app)

    body = client.get("/safety").json()

    assert body["live_trading"] is False
    assert body["paper_trading"] is True
    assert body["live_order_code_present"] is False


def test_4_market_data_failure_is_handled_safely(monkeypatch):
    """
    Реальный сбой сети/биржи (ConnectionError) обязан давать безопасный
    NO_TRADE, а не HTTP 500 и не необработанное исключение.
    """
    from api.server import app
    client = TestClient(app)

    def _raise(**kw):
        raise ConnectionError("simulated exchange outage")

    monkeypatch.setattr(DataEngine, "load", staticmethod(_raise))

    r = client.get("/paper/tick")

    assert r.status_code == 200
    assert r.json()["decision"]["decision"] == "NO_TRADE"


def test_5_one_monitor_cycle_executes(monkeypatch):
    """Один тик SchedulerLoop (тот же путь, что использует cloud monitor) выполняется успешно."""
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

    reconciler = OrderReconciler(te.broker, te.idempotency_store)
    loop = SchedulerLoop(
        exchange="binance", symbol="BTCUSDT", interval="5m", limit=300,
        adapter=te.broker, trade_engine=te.TradeEngine, reconciler=reconciler,
        replay_mode=True,
    )

    result = loop.run_once()

    assert result["decision"] is not None
    assert "error" not in result


def test_6_virtual_order_never_reaches_a_real_exchange(monkeypatch):
    """
    Полный цикл (включая TRADE-путь) исполняется только через PaperBroker.
    Ни binance.py, ни bybit.py не содержат ни одного метода для создания
    реального ордера -- это структурный, а не поведенческий факт,
    проверяемый здесь напрямую.
    """
    import inspect
    from api import binance, bybit

    order_like_names = ("create_order", "place_order", "new_order", "submit_order")

    for module in (binance.BinanceAPI, bybit.BybitAPI):
        methods = {name for name, _ in inspect.getmembers(module, predicate=inspect.isfunction)}
        for forbidden in order_like_names:
            assert forbidden not in methods, f"{module.__name__} unexpectedly defines {forbidden}"

    # And functionally: a TRADE decision only ever touches the paper broker.
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    from api.server import app
    client = TestClient(app)

    r = client.get("/paper/tick", params={"replay": "true"})
    body = r.json()

    assert body["execution"]["status"] == "OPENED"
    assert PositionManager.has_open_position() is True


def test_7_restart_does_not_duplicate_the_decision(monkeypatch):
    """
    Replaying the exact same signal after a simulated restart (in-memory
    PositionManager wiped, on-disk idempotency survives) must never open
    a second position.
    """
    from api.risk_engine import DailyRiskGuard

    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    from api.server import app
    client = TestClient(app)

    first = client.get("/paper/tick", params={"replay": "true"}).json()
    assert first["execution"]["status"] == "OPENED"

    # Simulate restart: in-memory position tracking lost, disk state survives.
    PositionManager.reset()
    DailyRiskGuard.reset()

    second = client.get("/paper/tick", params={"replay": "true"}).json()

    assert second["execution"]["status"] != "OPENED"
    assert PositionManager.has_open_position() is False
