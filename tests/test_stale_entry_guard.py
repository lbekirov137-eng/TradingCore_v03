"""
Регрессия: вход по устаревшему уровню давал фиктивный результат +3R.

Корень дефекта: уровни VLAD_ORB вычисляются один раз за сессию из
исторической свечи ретеста (orb_candidate_generator.py:349,
entry = retest["close"]) и дальше не пересчитываются. TradePlanStep
принимал их без сверки с рынком, поэтому позиция «открывалась» по цене,
которой на рынке давно нет.

Так как take_profit_2 = entry + 3R, а рынок уже был выше цели, позиция
закрывалась на первой же переоценке. Результат оказывался алгебраически
предопределён:

    PnL = 3R * quantity = 3R * (risk_amount / R) = 3 * risk_amount

то есть ровно +3.00 при risk_amount = 1.0 — независимо от рынка.

Тесты ниже закрывают три барьера:
  1) TradePlanStep не строит план по устаревшему уровню;
  2) PaperPositionManager не принимает недостижимый филл (защита в глубину);
  3) один сигнал не открывает позицию повторно.
"""

from pathlib import Path

import pytest

from api.contracts.context import MarketContext
from api.paper_trading.position_manager import PaperPositionManager
from api.pipeline_v2.steps.trade_plan_step import TradePlanStep
from paper_live_loop import extract_signal_id


ENTRY = 63462.33
STOP = 63392.39
RISK_PER_UNIT = ENTRY - STOP          # 69.94
TP1 = ENTRY + 2 * RISK_PER_UNIT       # 63602.21
TP2 = ENTRY + 3 * RISK_PER_UNIT       # 63672.15


def build_context(market_price: float) -> MarketContext:
    context = MarketContext()
    context.exchange = "binance"
    context.symbol = "BTCUSDT"
    context.timeframe = "5m"

    context.market = {"price": market_price}

    context.strategy = {
        "selected_trade": {
            "strategy": "EMA_AND_VLAD_ORB",
            "signal": "BUY",
            "entry": ENTRY,
            "stop": STOP,
            "take_profit_1": TP1,
            "take_profit_2": TP2,
            "risk_reward": "1:2 / 1:3",
        }
    }

    context.risk = {
        "allowed": True,
        "position_size": 0.014298,
        "risk_amount": 1.0,
        "risk_percent": 0.1,
        "execution_mode": "SPOT_LONG_ONLY",
    }

    return context


class TestTradePlanRejectsStaleEntry:

    def test_reproduces_the_defect_market_far_above_entry(self):
        """
        Именно этот случай наблюдался в облаке: рынок 64043.26 против
        входа 63462.33, расхождение +580.93 = 8.3R. До исправления план
        принимался и позиция мгновенно «зарабатывала» +3.00.
        """
        context = build_context(market_price=64043.26)

        result = TradePlanStep().process(context)
        plan = result.execution["trade_plan"]

        assert plan["allowed"] is False
        assert "ENTRY_LEVEL_NO_LONGER_VALID" in plan["reason"]
        assert plan["entry"] is None
        assert plan["risk_reward"] == "NO TRADE"

    def test_market_far_below_entry_is_also_rejected(self):
        """Расхождение вниз так же недопустимо, как и вверх."""
        context = build_context(market_price=ENTRY - 8 * RISK_PER_UNIT)

        plan = TradePlanStep().process(context).execution["trade_plan"]

        assert plan["allowed"] is False
        assert "ENTRY_LEVEL_NO_LONGER_VALID" in plan["reason"]

    def test_price_at_entry_is_accepted(self):
        """Свежий сигнал обязан проходить — иначе торговли не будет вовсе."""
        context = build_context(market_price=ENTRY)

        plan = TradePlanStep().process(context).execution["trade_plan"]

        assert plan["allowed"] is True
        assert plan["entry"] == ENTRY
        assert plan["take_profit_2"] == TP2

    def test_drift_just_inside_tolerance_is_accepted(self):
        context = build_context(
            market_price=ENTRY + 0.49 * RISK_PER_UNIT
        )

        plan = TradePlanStep().process(context).execution["trade_plan"]

        assert plan["allowed"] is True

    def test_drift_just_outside_tolerance_is_rejected(self):
        context = build_context(
            market_price=ENTRY + 0.51 * RISK_PER_UNIT
        )

        plan = TradePlanStep().process(context).execution["trade_plan"]

        assert plan["allowed"] is False

    @pytest.mark.parametrize(
        "bad_price",
        [None, float("nan"), float("inf"), "63500", True],
    )
    def test_unverifiable_market_price_fails_closed(self, bad_price):
        """
        Неизвестное состояние не считается безопасным: если цену нельзя
        прочитать как конечное число, вход запрещается, а не разрешается
        по умолчанию.
        """
        context = build_context(market_price=0.0)
        context.market = {"price": bad_price}

        plan = TradePlanStep().process(context).execution["trade_plan"]

        assert plan["allowed"] is False
        assert "ENTRY_LEVEL_UNVERIFIABLE" in plan["reason"]


class TestPositionManagerRejectsUnreachableFill:
    """Второй барьер: менеджер не должен принимать недостижимый филл."""

    def build_order(self) -> dict:
        return {
            "mode": "PAPER",
            "status": "FILLED_SIMULATED",
            "real_order_sent": False,
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "signal": "BUY",
            "side": "LONG",
            "entry": ENTRY,
            "stop": STOP,
            "take_profit_1": TP1,
            "take_profit_2": TP2,
            "quantity": 0.014298,
            "risk_amount": 1.0,
            "execution_mode": "SPOT_LONG_ONLY",
        }

    def manager(self, tmp_path: Path) -> PaperPositionManager:
        return PaperPositionManager(state_file=tmp_path / "pos.json")

    def test_unreachable_entry_is_refused(self, tmp_path):
        event = self.manager(tmp_path).open_position(
            self.build_order(),
            opened_at_utc="2026-07-28T17:00:00+00:00",
            market_price=64043.26,
        )

        assert event["event"] == "NO_POSITION_OPENED"
        assert "ENTRY_LEVEL_NO_LONGER_VALID" in event["reason"]
        assert event["real_order_sent"] is False

    def test_no_position_is_persisted_after_refusal(self, tmp_path):
        manager = self.manager(tmp_path)

        manager.open_position(
            self.build_order(),
            market_price=64043.26,
        )

        assert manager.has_open_position() is False

    def test_reachable_entry_is_accepted(self, tmp_path):
        event = self.manager(tmp_path).open_position(
            self.build_order(),
            opened_at_utc="2026-07-28T17:00:00+00:00",
            market_price=ENTRY,
        )

        assert event["event"] == "POSITION_OPENED"
        assert event["real_order_sent"] is False

    def test_omitting_market_price_keeps_backward_compatibility(
        self, tmp_path
    ):
        """Существующие вызовы без market_price должны продолжать работать."""
        event = self.manager(tmp_path).open_position(
            self.build_order(),
            opened_at_utc="2026-07-28T17:00:00+00:00",
        )

        assert event["event"] == "POSITION_OPENED"


class TestPnlIsNotAlgebraicallyFixed:
    """
    Детектор исходной патологии: результат не должен равняться
    3 * risk_amount независимо от рынка.
    """

    def test_realized_pnl_depends_on_actual_exit_price(self, tmp_path):
        manager = PaperPositionManager(state_file=tmp_path / "pos.json")

        manager.open_position(
            {
                "mode": "PAPER",
                "status": "FILLED_SIMULATED",
                "real_order_sent": False,
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "signal": "BUY",
                "side": "LONG",
                "entry": ENTRY,
                "stop": STOP,
                "take_profit_1": TP1,
                "take_profit_2": TP2,
                "quantity": 0.014298,
                "risk_amount": 1.0,
                "execution_mode": "SPOT_LONG_ONLY",
            },
            opened_at_utc="2026-07-28T17:00:00+00:00",
            market_price=ENTRY,
        )

        # Выход по стопу обязан дать УБЫТОК, а не предопределённые +3.00.
        event = manager.evaluate_position(
            market_price=STOP,
            candle_high=ENTRY,
            candle_low=STOP - 1.0,
            observed_at_utc="2026-07-28T17:05:00+00:00",
        )

        assert event["event"] == "POSITION_CLOSED"

        position = event["position"]
        assert position["exit_reason"] == "STOP_LOSS"
        assert position["realized_pnl"] < 0
        assert position["realized_pnl"] != pytest.approx(3.0)


class TestSignalIsNotReused:

    def build_pipeline_data(self, retest_time: str) -> dict:
        return {
            "strategy": {
                "vlad_orb_candidate": {
                    "session_date": "2026-07-28",
                    "retest": {"time": retest_time, "close": ENTRY},
                }
            }
        }

    def test_signal_id_is_stable_for_the_same_signal(self):
        first = extract_signal_id(
            self.build_pipeline_data("2026-07-28T10:50:00-04:00")
        )
        second = extract_signal_id(
            self.build_pipeline_data("2026-07-28T10:50:00-04:00")
        )

        assert first == second
        assert first == "2026-07-28:2026-07-28T10:50:00-04:00"

    def test_signal_id_changes_for_a_new_retest(self):
        first = extract_signal_id(
            self.build_pipeline_data("2026-07-28T10:50:00-04:00")
        )
        second = extract_signal_id(
            self.build_pipeline_data("2026-07-28T11:20:00-04:00")
        )

        assert first != second

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"strategy": {}},
            {"strategy": {"vlad_orb_candidate": {}}},
            {"strategy": {"vlad_orb_candidate": {"session_date": "2026-07-28"}}},
        ],
    )
    def test_missing_data_yields_no_signal_id(self, payload):
        """Без идентификатора дедупликация просто не применяется."""
        assert extract_signal_id(payload) is None


class TestLoopDoesNotReopenOnSpentSignal:
    """
    Проверка самого гейта в цикле: тот же сигнал на СЛЕДУЮЩЕЙ свече не
    должен открывать позицию повторно. Дедупликация по свече здесь не
    помогает — свеча действительно новая, устаревшим является сигнал.
    """

    def build_result(self):
        result = MarketContext()
        result.exchange = "binance"
        result.symbol = "BTCUSDT"
        result.timeframe = "5m"
        result.market = {"price": ENTRY}
        result.indicators = {}
        result.regime = {}
        result.risk = {"risk_amount": 1.0}
        result.decision = {"decision": "TRADE"}
        result.audit = {}
        result.strategy = {
            "vlad_orb_candidate": {
                "session_date": "2026-07-28",
                "retest": {"time": "2026-07-28T10:50:00-04:00", "close": ENTRY},
            }
        }
        result.execution = {
            "paper_order": {
                "mode": "PAPER",
                "status": "FILLED_SIMULATED",
                "real_order_sent": False,
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "signal": "BUY",
                "side": "LONG",
                "entry": ENTRY,
                "stop": STOP,
                "take_profit_1": TP1,
                "take_profit_2": TP2,
                "quantity": 0.014298,
                "risk_amount": 1.0,
                "execution_mode": "SPOT_LONG_ONLY",
            }
        }
        return result

    def run_candle(self, tmp_path, monkeypatch, used_signal_id):
        import paper_live_loop as loop

        monkeypatch.setattr(
            loop, "build_unified_market_context", lambda **kw: {}
        )
        monkeypatch.setattr(loop, "review_closed_candle", lambda **kw: {})
        monkeypatch.setattr(loop, "run_shadow_filter", lambda **kw: {})

        result = self.build_result()

        class Engine:
            def execute(self, context):
                return result

        snapshot = {
            "price": ENTRY,
            "candle_high": ENTRY + 1,
            "candle_low": ENTRY - 1,
            "close_time_ms": 1_700_000_000_000,
        }

        return loop.process_closed_candle(
            engine=Engine(),
            position_manager=PaperPositionManager(
                state_file=tmp_path / "pos.json"
            ),
            context=result,
            snapshot=snapshot,
            used_signal_id=used_signal_id,
        )

    def test_fresh_signal_opens_position(self, tmp_path, monkeypatch):
        event, _ = self.run_candle(tmp_path, monkeypatch, used_signal_id=None)

        assert event["event"] == "POSITION_OPENED"
        assert event["signal_id"] == "2026-07-28:2026-07-28T10:50:00-04:00"

    def test_spent_signal_does_not_reopen(self, tmp_path, monkeypatch):
        event, _ = self.run_candle(
            tmp_path,
            monkeypatch,
            used_signal_id="2026-07-28:2026-07-28T10:50:00-04:00",
        )

        assert event["event"] == "NO_POSITION_OPENED"
        assert "SIGNAL_ALREADY_USED" in event["reason"]
        assert event["real_order_sent"] is False
