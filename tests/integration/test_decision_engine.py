from api.contracts.context import LiveContext
from api.decision_engine.decision_engine import DecisionEngine
from api.position_manager.position_manager import PositionManager
from api.risk_engine import DailyRiskGuard
from api.risk.guards import LossStreakGuard, CooldownAfterLossGuard, DailyLossGuard, MaxTradesPerSessionGuard


def _approved_signal(entry=100.25, stop=99.748, tp1=101.254, tp2=101.756):
    return {
        "approved": True,
        "strategy": "ORB",
        "direction": "LONG",
        "trade_plan": {
            "entry": entry,
            "stop_loss": stop,
            "take_profit": {"tp1": tp1, "tp2": tp2, "risk_reward": "1:2 / 1:3"},
            "risk_reward": "1:2 / 1:3",
        },
        "confidence": 0.5,
        "reason": "Пробой и ретест подтверждены.",
        "metadata": {"opening_range": {"session": "CRYPTO", "timestamp": 111}},
    }


def _ctx_with_signal(signal):
    ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
    ctx.strategy_signals = [signal]
    return ctx


class TestDecisionEngineDefaultsSafe:

    def test_no_signals_gives_no_trade(self):
        ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
        ctx.strategy_signals = []

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"

    def test_unapproved_signal_gives_no_trade_with_reason_passthrough(self):
        signal = {"approved": False, "strategy": "ORB", "reason": "Нет пробоя."}
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert decision["reason"] == "Нет пробоя."

    def test_incomplete_trade_plan_gives_no_trade_not_crash(self):
        signal = _approved_signal()
        signal["trade_plan"]["stop_loss"] = None

        ctx = _ctx_with_signal(signal)
        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"


class TestDecisionEngineRiskReward:

    def test_rejects_when_rr_below_minimum(self):
        # tp1 too close to entry -> RR < 2
        signal = _approved_signal(entry=100.0, stop=99.0, tp1=100.5, tp2=101.0)
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "R:R" in decision["reason"]

    def test_approves_when_rr_meets_minimum(self):
        signal = _approved_signal(entry=100.0, stop=99.0, tp1=102.0, tp2=103.0)  # RR exactly 2
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "TRADE"
        assert decision["exchange"] == "binance"
        assert decision["symbol"] == "BTCUSDT"
        assert decision["strategy"] == "ORB"
        assert decision["risk_reward_ratio"] == 2.0


class TestDecisionEngineSafetyGates:

    def test_blocks_when_position_already_open(self):
        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="existing")

        signal = _approved_signal()
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "открытых позиций" in decision["reason"]

    def test_blocks_when_daily_trade_limit_reached(self):
        DailyRiskGuard._trades_today = 999_999  # force limit without depending on config value
        DailyRiskGuard._date = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).date()

        signal = _approved_signal()
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"

    def test_risk_is_computed_from_real_stop_distance_not_raw_atr(self):
        """
        CRITICAL: risk must be sized from |entry - stop| (plus the slippage
        buffer applied by PositionSizer), not from ATR directly (ORB stop !=
        ATR by construction: stop = range_edge +/- 0.2*ATR).
        """
        from config.settings import DEFAULT_SLIPPAGE_BPS

        signal = _approved_signal(entry=100.0, stop=98.0, tp1=104.0, tp2=106.0)  # raw risk_distance=2.0
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "TRADE"

        slippage_amount = 100.0 * (DEFAULT_SLIPPAGE_BPS / 10_000)
        expected_effective_stop_distance = 2.0 + 2 * slippage_amount

        assert decision["risk"]["stop_distance"] == round(expected_effective_stop_distance, 8)


class TestDecisionEngineWiredGuards:
    """
    Every guard listed in Priority 3 must actually gate DecisionEngine.decide,
    not just exist as dead code (see ISSUES.md H-6, now resolved).
    """

    def test_loss_streak_guard_blocks_new_entries(self):
        for _ in range(3):
            LossStreakGuard.register_result(-1.0)

        signal = _approved_signal()
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "Серия убытков" in decision["reason"]

    def test_cooldown_after_loss_guard_blocks_new_entries(self):
        CooldownAfterLossGuard.register_result(-1.0)

        signal = _approved_signal()
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "Пауза после убыточной" in decision["reason"]

    def test_daily_loss_guard_blocks_new_entries(self):
        from config.settings import DEFAULT_BALANCE, MAX_DAILY_LOSS_PERCENT

        DailyLossGuard.register_result(-(DEFAULT_BALANCE * MAX_DAILY_LOSS_PERCENT / 100) - 1)

        signal = _approved_signal()
        ctx = _ctx_with_signal(signal)

        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "Реализованный убыток" in decision["reason"]

    def test_max_trades_per_session_guard_blocks_second_trade_same_session(self):
        signal = _approved_signal()
        session_key = (
            "binance", "BTCUSDT", "ORB",
            signal["metadata"]["opening_range"]["session"],
            signal["metadata"]["opening_range"]["timestamp"],
        )
        MaxTradesPerSessionGuard.register_trade(session_key)

        ctx = _ctx_with_signal(signal)
        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert "уже" in decision["reason"]

    def test_every_blocked_trade_has_a_machine_readable_reason(self):
        """
        Sanity check across every gate: NO_TRADE must always carry a
        non-empty string reason, never None or empty.
        """
        for _ in range(3):
            LossStreakGuard.register_result(-1.0)

        signal = _approved_signal()
        ctx = _ctx_with_signal(signal)
        decision = DecisionEngine.decide(ctx)

        assert decision["decision"] == "NO_TRADE"
        assert isinstance(decision["reason"], str)
        assert len(decision["reason"]) > 0
