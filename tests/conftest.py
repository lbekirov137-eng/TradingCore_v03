import sys
import os
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.market_data.market_snapshot import MarketSnapshot
from api.position_manager.position_manager import PositionManager
from api.risk_engine import DailyRiskGuard
from api.decision_engine.decision_engine import kill_switch
from api.risk import guards as risk_guards
from config.settings import DEFAULT_BALANCE


# 2024-01-10 00:00:00 UTC — за пределами London (08-16) и New York (9:30-16)
# сессий, поэтому SessionResolver детерминированно даёт CRYPTO для любого
# теста, независимо от реального времени запуска.
QUIET_UTC_MIDNIGHT_MS = int(
    datetime(2024, 1, 10, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
)

FIVE_MIN_MS = 5 * 60 * 1000


@pytest.fixture(scope="session", autouse=True)
def _isolate_runtime_state(tmp_path_factory):
    """
    Перенаправляет ВСЁ дисковое состояние (paper-брокер, идемпотентность,
    позиции, kill switch) во временную директорию на время тестов.

    Без этого тесты читали/писали реальный `state/` в корне репозитория:
    состояние от предыдущего прогона просачивалось в следующий, и
    детерминированные client_order_id корректно (но неожиданно для теста)
    распознавались как "этот ордер уже отправлялся". Изоляция делает
    прогон воспроизводимым и не зависящим от истории запусков.
    """
    from api.trade_engine import trade_engine as te
    from api.decision_engine.decision_engine import kill_switch
    from api.position_manager.position_manager import PositionManager as PM
    from api.observability.paper_forward_journal import journal as pfj

    root = tmp_path_factory.mktemp("tradingcore_state")

    te.broker.state_path = str(root / "paper_broker.json")
    te.idempotency_store.state_dir = str(root / "orders")
    os.makedirs(te.idempotency_store.state_dir, exist_ok=True)
    kill_switch.state_path = str(root / "kill_switch.json")
    PM._state_path = str(root / "position_manager.json")
    pfj.path = str(root / "paper_forward_journal.jsonl")

    yield


def _reset_execution_singletons():
    """Обнуляет paper-брокер, журнал, хранилище идемпотентности и health-трекер между тестами."""
    from api.trade_engine import trade_engine as te
    from api.observability.states import health

    te.broker.reset(initial_balance=DEFAULT_BALANCE)
    te.idempotency_store.reset()
    te.journal.trades.clear()
    health.reset()


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Обнуляет процесс-глобальное состояние перед и после каждого теста."""
    PositionManager.reset()
    DailyRiskGuard.reset()
    kill_switch.reset()
    risk_guards.reset_all()
    _reset_execution_singletons()
    yield
    PositionManager.reset()
    DailyRiskGuard.reset()
    kill_switch.reset()
    risk_guards.reset_all()
    _reset_execution_singletons()


def make_snapshot(closes, highs=None, lows=None, opens=None, volumes=None,
                   start_ms=QUIET_UTC_MIDNIGHT_MS, step_ms=FIVE_MIN_MS,
                   exchange="binance", symbol="BTCUSDT", interval="5m"):
    """Строит детерминированный MarketSnapshot для юнит/regression тестов."""

    n = len(closes)
    timestamps = [start_ms + i * step_ms for i in range(n)]

    if highs is None:
        highs = [c + 0.1 for c in closes]
    if lows is None:
        lows = [c - 0.1 for c in closes]
    if opens is None:
        opens = list(closes)
    if volumes is None:
        volumes = [100.0 for _ in closes]

    return MarketSnapshot(
        exchange=exchange,
        symbol=symbol,
        interval=interval,
        timestamps=timestamps,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
    )


def flat_series(n, price=100.0, noise=0.05):
    """Почти плоский, но не строго нулевой ATR ряд (нужен ненулевой ATR)."""
    out = []
    for i in range(n):
        out.append(price + (noise if i % 2 == 0 else -noise))
    return out


def orb_breakout_snapshot(breakout=True, n_filler=24):
    """
    Детерминированный сценарий ORB LONG breakout+retest на сессии CRYPTO:
    5 свечей opening range (high=100.2/low=99.8), n_filler свечей внутри
    диапазона (нужны только чтобы ATR(14) не был NaN), последняя свеча
    пробивает и одновременно ретестит верхнюю границу.

    breakout=False строит серию, где пробоя не происходит (слабый сигнал).
    """

    closes, highs, lows, opens = [], [], [], []

    for _ in range(5):
        closes.append(100.0); highs.append(100.2); lows.append(99.8); opens.append(100.0)

    for i in range(n_filler):
        c = 100.05 if i % 2 == 0 else 99.95
        closes.append(c); highs.append(100.1); lows.append(99.9); opens.append(c)

    if breakout:
        closes.append(100.25); highs.append(100.3); lows.append(100.0); opens.append(100.05)
    else:
        closes.append(100.05); highs.append(100.15); lows.append(99.95); opens.append(100.0)

    return make_snapshot(closes, highs=highs, lows=lows, opens=opens)
