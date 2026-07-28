"""
Детерминированный end-to-end paper/dry-run прогон через реальный
Workflow.run (Scheduler -> StrategyEngine(ORB) -> DecisionEngine ->
TradeEngine), с подменённым DataEngine.load (без обращений к сети).

Покрывает обязательные сценарии из ТЗ:
a) качественный сигнал -> TRADE + OPENED
b) слабый сигнал -> NO_TRADE
c) stale/missing market data -> безопасный NO_TRADE
d) ошибка API (сеть/биржа) -> безопасный NO_TRADE, без падения процесса
e) неверный/недостаточный размер данных для риска (NaN ATR) -> NO_TRADE
f) превышение дневного риска/лимита сделок -> NO_TRADE
g) повторный ордер (та же сессия/сигнатура) -> FAILED_SAFELY, без второй позиции
h) безопасный restart/resume -> PositionManager.reset() очищает состояние,
   новая сделка после рестарта обрабатывается корректно
"""

import pytest

from api.contracts.context import LiveContext
from api.workflow.workflow import Workflow
from api.data_engine import DataEngine
from api.position_manager.position_manager import PositionManager
from api.risk_engine import DailyRiskGuard
from api.market_data.candle_utils import StaleMarketDataError

from tests.conftest import orb_breakout_snapshot, make_snapshot


def _new_context():
    # replay_mode=True: фикстуры используют детерминированные исторические
    # timestamps, поэтому «сейчас» берётся из последней свечи. В реальном
    # paper-forward запуске replay_mode остаётся False и stale-фильтр
    # работает по настоящим часам.
    return LiveContext(
        exchange="binance", symbol="BTCUSDT", interval="5m", limit=300,
        replay_mode=True,
    )


def test_a_good_signal_opens_paper_trade(monkeypatch):
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    result = Workflow.run(_new_context())

    assert result["decision"]["decision"] == "TRADE"
    assert result["execution"]["status"] == "OPENED"
    assert PositionManager.has_open_position() is True


def test_b_weak_signal_gives_no_trade(monkeypatch):
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=False)))

    result = Workflow.run(_new_context())

    assert result["decision"]["decision"] == "NO_TRADE"
    assert result["execution"]["status"] == "NO_TRADE"
    assert PositionManager.has_open_position() is False


def test_c_stale_or_missing_data_gives_safe_no_trade(monkeypatch):
    def _raise(**kw):
        raise StaleMarketDataError("свечи не проходят валидацию")

    monkeypatch.setattr(DataEngine, "load", staticmethod(_raise))

    result = Workflow.run(_new_context())

    assert result["decision"]["decision"] == "NO_TRADE"
    assert "проверку" in result["decision"]["reason"] or "Безопасная" in result["decision"]["reason"]
    assert PositionManager.has_open_position() is False


def test_d_exchange_api_error_gives_safe_no_trade_not_crash(monkeypatch):
    def _raise(**kw):
        raise ConnectionError("биржа недоступна")

    monkeypatch.setattr(DataEngine, "load", staticmethod(_raise))

    result = Workflow.run(_new_context())  # must not raise

    assert result["decision"]["decision"] == "NO_TRADE"
    assert "Безопасная остановка" in result["decision"]["reason"]
    assert PositionManager.has_open_position() is False


def test_e_insufficient_data_for_atr_gives_no_trade(monkeypatch):
    monkeypatch.setattr(
        DataEngine, "load",
        staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True, n_filler=0)),
    )

    result = Workflow.run(_new_context())

    assert result["decision"]["decision"] == "NO_TRADE"
    assert PositionManager.has_open_position() is False


def test_f_daily_trade_limit_blocks_further_trades(monkeypatch):
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    # Consume the daily quota directly (avoids depending on exact config value).
    from config.settings import MAX_DAILY_TRADES
    for _ in range(MAX_DAILY_TRADES):
        DailyRiskGuard.register_trade(risk_amount=0.01)

    PositionManager.reset()  # ensure "one open position" gate isn't what blocks it

    result = Workflow.run(_new_context())

    assert result["decision"]["decision"] == "NO_TRADE"
    assert "лимит" in result["decision"]["reason"]


def test_g_duplicate_order_is_blocked_safely(monkeypatch):
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    first = Workflow.run(_new_context())
    assert first["execution"]["status"] == "OPENED"

    # Close so a duplicate SIGNATURE (not just "position already open") is what's tested.
    PositionManager.close_position("test-close")

    second = Workflow.run(_new_context())

    assert second["decision"]["decision"] == "NO_TRADE"
    assert "сессия уже отторгована" in second["decision"]["reason"]
    # No phantom second position was created.
    assert PositionManager.has_open_position() is False


def test_h_restart_replaying_same_signal_never_opens_a_second_position(monkeypatch):
    """
    CRITICAL restart-recovery invariant.

    An earlier version of this test asserted that after a restart the very
    same signal would open a NEW position. That assertion encoded unsafe
    behavior: in a real deployment the first order was already filled on
    the exchange, so replaying it would silently double the position.

    Correct behavior (idempotency + exchange-state-is-source-of-truth):
    the deterministic client_order_id is recognized as already submitted,
    so execution reports ORDER_PENDING / already-tracked and NO second
    position is created — even though the in-memory PositionManager was
    wiped by the restart.
    """
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    first = Workflow.run(_new_context())
    assert first["execution"]["status"] == "OPENED"
    assert PositionManager.has_open_position() is True

    # Simulate process restart: in-memory position state is lost, but the
    # on-disk idempotency store (order history) survives, as it would in production.
    PositionManager.reset()
    DailyRiskGuard.reset()
    assert PositionManager.has_open_position() is False

    second = Workflow.run(_new_context())

    assert second["execution"]["status"] == "ORDER_PENDING"
    assert "уже отслеживается" in second["execution"]["reason"]
    # The critical part: no phantom duplicate position was opened.
    assert PositionManager.has_open_position() is False


def test_h2_restart_then_genuinely_new_signal_can_still_trade(monkeypatch):
    """
    The idempotency guard must block only REPLAYS, not legitimately new
    trades. A different session (different opening range timestamp) yields
    a different client_order_id and must be allowed to open normally.
    """
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    first = Workflow.run(_new_context())
    assert first["execution"]["status"] == "OPENED"

    # Restart, and this time a genuinely different session's data arrives.
    PositionManager.reset()
    DailyRiskGuard.reset()

    shifted = orb_breakout_snapshot(breakout=True)
    day_ms = 24 * 60 * 60 * 1000
    shifted.timestamps = [ts + day_ms for ts in shifted.timestamps]
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: shifted))

    second = Workflow.run(_new_context())

    assert second["execution"]["status"] == "OPENED"
    assert PositionManager.has_open_position() is True
