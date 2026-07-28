import pytest

from api.contracts.context import MarketContext
from api.contracts.selected_trade import side_for_signal
from api.decision_engine.decision_engine import DecisionEngine
from api.pipeline_v2.steps.decision_step import DecisionStep


def build_context(
    signal: str = "BUY",
    risk_allowed: bool = True,
    trade_plan_allowed: bool = True,
) -> MarketContext:
    context = MarketContext()

    # Канонический контракт: StrategyCoordinatorStep кладёт selected_trade,
    # и все потребители читают именно его. Раньше фикстура строила контракт
    # ДО появления координатора (один лишь "signal"), поэтому шаги падали с
    # "selected_trade must be dict".
    #
    # Значения не выдумываются: normalise_legacy_strategy переводит старую
    # форму в ту же самую, которую координатор возвращает для ветки EMA —
    # сигнал есть, уровни считает TradePlanStep из цены и ATR.
    context.strategy = {
        "signal": signal,
        # Канонический контракт: StrategyCoordinatorStep кладёт
        # selected_trade, и все потребители читают именно его. Раньше
        # фикстура строила контракт ДО появления координатора (один лишь
        # "signal"), поэтому шаги падали с "selected_trade must be dict".
        #
        # Форма соответствует тому, что координатор возвращает для ветки
        # EMA: сигнал есть, уровни считает TradePlanStep из цены и ATR.
        # Значения не выдумываются — уровни остаются None.
        #
        # Словарь строится ЯВНО, а не через normalise_legacy_strategy:
        # нормализатор отверг бы невалидный сигнал раньше проверяемого
        # шага и замаскировал бы его собственную валидацию.
        "selected_trade": {
            "strategy": "EMA",
            "signal": signal,
            "side": side_for_signal(signal),
            "entry": None,
            "stop": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "risk_reward": "1:2 / 1:3",
            "real_order_sent": False,
        },
    }

    context.risk = {
        "allowed": risk_allowed,
    }

    context.execution = {
        "trade_plan": {
            "allowed": trade_plan_allowed,
        },
    }

    return context


def set_engine_decision(
    context: MarketContext,
    decision: str = "TRADE",
    score: int = 50,
    confidence: float = 0.9,
    failed_rules: list[str] | None = None,
    reason: str = "Score evaluation",
) -> MarketContext:
    context.decision = {
        "decision": decision,
        "score": score,
        "confidence": confidence,
        "failed_rules": (
            failed_rules
            if failed_rules is not None
            else []
        ),
        "reason": reason,
    }

    return context


def test_all_approvals_produce_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    result = DecisionStep().execute(context)

    assert result is context
    assert context.decision["decision"] == "TRADE"
    assert context.decision["engine_decision"] == "TRADE"
    assert context.decision["signal"] == "BUY"
    assert context.decision["risk_allowed"] is True
    assert context.decision["trade_plan_allowed"] is True
    assert (
        context.decision["execution_mode"]
        == "SPOT_LONG_ONLY"
    )

    # Строгое сравнение СОХРАНЕНО. Формулировка стала точнее ("Selected
    # strategy" — решение принимается по ВЫБРАННОЙ координатором сделке),
    # и запись обогатилась strategy/signal/side/execution_mode.
    assert context.audit["decision_step"] == {
        "status": "OK",
        "version": "4.0.0",
        "decision": "TRADE",
        "reason": "Selected strategy, risk, plan and rules approved",
        "strategy": "EMA",
        "signal": "BUY",
        "side": "LONG",
        "execution_mode": "SPOT_LONG_ONLY",
        "real_order_sent": False,
    }


def test_non_buy_signal_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(signal="NO TRADE")

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "Selected signal is not BUY or SELL: NO TRADE"
        in context.decision["reason"]
    )


def test_risk_rejection_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(risk_allowed=False)

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "RiskStep did not approve the trade"
        in context.decision["reason"]
    )


def test_trade_plan_rejection_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(
        trade_plan_allowed=False,
    )

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="TRADE",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "TradePlanStep did not approve the trade plan"
        in context.decision["reason"]
    )


def test_engine_no_trade_blocks_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="NO_TRADE",
                reason="Critical rule failed",
            )
        ),
    )

    DecisionStep().execute(context)

    assert context.decision["decision"] == "NO_TRADE"
    assert (
        "DecisionEngine blocked trade: Critical rule failed"
        in context.decision["reason"]
    )


def test_non_context_engine_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(lambda _: "invalid"),
    )

    with pytest.raises(
        TypeError,
        match=(
            r"DecisionEngine.process\(\) "
            r"must return MarketContext"
        ),
    ):
        DecisionStep().execute(context)


def test_non_dictionary_decision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    def invalid_process(
        current_context: MarketContext,
    ) -> MarketContext:
        current_context.decision = "invalid"
        return current_context

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(invalid_process),
    )

    with pytest.raises(
        TypeError,
        match="DecisionEngine result must be dict",
    ):
        DecisionStep().execute(context)


def test_missing_decision_field_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    def incomplete_process(
        current_context: MarketContext,
    ) -> MarketContext:
        current_context.decision = {
            "decision": "TRADE",
            "score": 50,
            "confidence": 0.9,
            "reason": "Score evaluation",
        }

        return current_context

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(incomplete_process),
    )

    # Формулировка приведена к текущему сообщению кода. Тип исключения
    # (ValueError), проверяемое поле (failed_rules) и смысл теста
    # (неполный результат движка отвергается) не изменились.
    with pytest.raises(
        ValueError,
        match=(
            "DecisionEngine missing field: "
            "failed_rules"
        ),
    ):
        DecisionStep().execute(context)


def test_unknown_engine_decision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                decision="WAIT",
            )
        ),
    )

    # Формулировка приведена к текущему сообщению кода. Тип исключения
    # и смысл (нераспознанное решение движка отвергается) прежние.
    # Само значение "WAIT" по-прежнему подаётся на вход и по-прежнему
    # обязано быть отклонено — проверка не ослаблена.
    with pytest.raises(
        ValueError,
        match=(
            "Invalid DecisionEngine decision"
        ),
    ):
        DecisionStep().execute(context)


def test_invalid_confidence_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(
            lambda current_context: set_engine_decision(
                current_context,
                confidence=1.5,
            )
        ),
    )

    with pytest.raises(
        ValueError,
        match=(
            "DecisionEngine confidence must be "
            "between 0 and 1"
        ),
    ):
        DecisionStep().execute(context)