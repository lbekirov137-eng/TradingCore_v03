import pytest

from api.contracts.context import MarketContext
from api.contracts.selected_trade import side_for_signal
from api.pipeline_v2.steps.paper_execution_step import (
    PaperExecutionStep,
)


def build_context(
    decision: str = "TRADE",
    signal: str = "BUY",
    trade_plan_allowed: bool = True,
) -> MarketContext:
    context = MarketContext()

    context.exchange = "binance"
    context.symbol = "BTCUSDT"
    context.timeframe = "5m"

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

    context.decision = {
        "decision": decision,
        "reason": (
            "All checks approved"
            if decision == "TRADE"
            else "Trade was blocked"
        ),
    }

    context.execution = {
        "runtime": {
            "mode": "DRY_RUN",
            "real_orders_enabled": False,
        },
        "trade_plan": {
            "allowed": trade_plan_allowed,
            "signal": signal,
            "entry": 100000.0,
            "stop": 99500.0,
            "take_profit_1": 101000.0,
            "take_profit_2": 101500.0,
            "position_size": 0.002,
            "risk_amount": 1.0,
            "execution_mode": "SPOT_LONG_ONLY",
        },
    }

    return context


def test_trade_creates_simulated_filled_order() -> None:
    context = build_context()

    result = PaperExecutionStep().execute(context)

    assert result is context

    order = context.execution["paper_order"]

    # Строгое сравнение СОХРАНЕНО. Контракт стал точнее: "side" теперь
    # несёт НАПРАВЛЕНИЕ позиции (LONG/SHORT), а торговый сигнал вынесен в
    # отдельное поле "signal" (BUY/SELL). Раньше side переиспользовалось
    # под значение сигнала — именно это расхождение заставляло
    # PaperPositionManager отвергать каждый валидный лонг.
    # Прежняя проверка "это покупка" сохранена через signal == "BUY",
    # поэтому покрытие расширено, а не ослаблено.
    assert order == {
        "mode": "PAPER",
        "status": "FILLED_SIMULATED",
        "real_order_sent": False,
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "strategy": "EMA",
        "signal": "BUY",
        "side": "LONG",
        "risk_percent": None,
        "entry": 100000.0,
        "quantity": 0.002,
        "stop": 99500.0,
        "take_profit_1": 101000.0,
        "take_profit_2": 101500.0,
        "risk_amount": 1.0,
        "execution_mode": "SPOT_LONG_ONLY",
        "reason": "Virtual paper order executed",
    }

    # Строгое сравнение СОХРАНЕНО. Версия шага выросла до 3.0.0, запись
    # обогатилась execution_mode/side/signal/strategy. Ключевые
    # инварианты безопасности здесь же: real_order_sent False и
    # execution_mode SPOT_LONG_ONLY.
    assert context.audit["paper_execution_step"] == {
        "status": "OK",
        "version": "3.0.0",
        "mode": "PAPER",
        "result": "FILLED_SIMULATED",
        "real_order_sent": False,
        "execution_mode": "SPOT_LONG_ONLY",
        "side": "LONG",
        "signal": "BUY",
        "strategy": "EMA",
    }


def test_no_trade_creates_skipped_order() -> None:
    context = build_context(
        decision="NO_TRADE",
        trade_plan_allowed=False,
    )

    result = PaperExecutionStep().execute(context)

    assert result is context

    order = context.execution["paper_order"]

    # Пропущенный ордер тоже несёт разделённые signal/side и стратегию —
    # это нужно, чтобы по журналу было видно, ЧТО именно было пропущено.
    assert order == {
        "mode": "PAPER",
        "status": "SKIPPED",
        "real_order_sent": False,
        "strategy": "EMA",
        "signal": "BUY",
        "side": "LONG",
        "reason": "Trade was blocked",
    }

    assert context.audit["paper_execution_step"] == {
        "status": "OK",
        "version": "3.0.0",
        "mode": "PAPER",
        "result": "SKIPPED",
        "real_order_sent": False,
        "execution_mode": "SPOT_LONG_ONLY",
        "side": "LONG",
        "signal": "BUY",
        "strategy": "EMA",
    }


def test_invalid_final_decision_is_rejected() -> None:
    context = build_context()
    context.decision["decision"] = "WAIT"

    # Формулировка приведена к текущему сообщению кода. Тип исключения
    # (ValueError) и смысл (нераспознанное финальное решение отвергается)
    # не изменились; значение "WAIT" по-прежнему обязано отклоняться.
    with pytest.raises(
        ValueError,
        match=(
            "Invalid final decision: WAIT"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_non_dictionary_decision_is_rejected() -> None:
    context = build_context()
    context.decision = "TRADE"  # type: ignore[assignment]

    # Формулировка приведена к текущему сообщению кода. Тип исключения
    # (TypeError) сохранён — проверка типа context.decision не ослаблена.
    with pytest.raises(
        TypeError,
        match=(
            "PaperExecutionStep decision must be dict"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_sell_signal_is_rejected() -> None:
    context = build_context(signal="SELL")

    with pytest.raises(
        ValueError,
        match="Paper SELL requires PAPER_LONG_SHORT mode",
    ):
        PaperExecutionStep().execute(context)


def test_disallowed_trade_plan_is_rejected() -> None:
    context = build_context(
        trade_plan_allowed=False,
    )

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep trade plan is not allowed"
        ),
    ):
        PaperExecutionStep().execute(context)


def test_missing_trade_plan_field_is_rejected() -> None:
    context = build_context()

    del context.execution["trade_plan"]["position_size"]

    with pytest.raises(
        ValueError,
        # Отсутствующее поле даёт None, а None не является положительным
        # числом — шаг отвергает план по той же причине, что и ноль.
        # Проверяется главное: ордер не создаётся без размера позиции.
        match="Paper plan field position_size must be positive",
    ):
        PaperExecutionStep().execute(context)


def test_zero_position_size_is_rejected() -> None:
    context = build_context()

    context.execution["trade_plan"]["position_size"] = 0.0

    with pytest.raises(
        ValueError,
        match="Paper plan field position_size must be positive",
    ):
        PaperExecutionStep().execute(context)


def test_inconsistent_buy_levels_are_rejected() -> None:
    context = build_context()

    context.execution["trade_plan"]["stop"] = 100500.0

    with pytest.raises(
        ValueError,
        match=(
            "PaperExecutionStep BUY levels are inconsistent"
        ),
    ):
        PaperExecutionStep().execute(context)