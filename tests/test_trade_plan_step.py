import pytest

from api.contracts.context import MarketContext
from api.contracts.selected_trade import side_for_signal
from api.pipeline_v2.steps.trade_plan_step import TradePlanStep
from api.trade_plan import TradePlan


def build_context(
    signal: str = "BUY",
    risk_allowed: bool = True,
    price: float = 100000.0,
    atr: float = 500.0,
) -> MarketContext:
    context = MarketContext()

    context.market = {
        "price": price,
    }

    context.indicators = {
        "atr": {
            "value": atr,
        },
    }

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
        "reason": (
            "Risk approved"
            if risk_allowed
            else "Risk was not approved"
        ),
        "risk_amount": 1.0 if risk_allowed else 0.0,
        "position_size": 0.002 if risk_allowed else 0.0,
        "stop_distance": atr,
        "risk_percent": 0.1,
        "execution_mode": "SPOT_LONG_ONLY",
    }

    return context


def test_approved_buy_creates_trade_plan() -> None:
    context = build_context()

    result = TradePlanStep().execute(context)

    assert result is context

    plan = context.execution["trade_plan"]

    assert plan["allowed"] is True
    assert plan["signal"] == "BUY"
    assert plan["entry"] == 100000.0
    assert plan["stop"] == 99500.0
    assert plan["take_profit_1"] == 101000.0
    assert plan["take_profit_2"] == 101500.0
    assert plan["risk_reward"] == "1:2 / 1:3"
    assert plan["position_size"] == 0.002
    assert plan["risk_amount"] == 1.0
    assert plan["execution_mode"] == "SPOT_LONG_ONLY"

    # Строгое сравнение СОХРАНЕНО. Обновлены фактические значения:
    # версия шага 4.1.0, формулировка "EMA ATR trade plan created"
    # отличает ATR-путь от плана по уровням стратегии, и запись
    # обогатилась strategy/signal/side/execution_mode.
    assert context.audit["trade_plan_step"] == {
        "status": "OK",
        "version": "4.1.0",
        "allowed": True,
        "reason": "EMA ATR trade plan created",
        "strategy": "EMA",
        "signal": "BUY",
        "side": "LONG",
        "execution_mode": "SPOT_LONG_ONLY",
        "real_order_sent": False,
    }


def test_blocked_risk_creates_no_trade_plan() -> None:
    context = build_context(
        signal="NO TRADE",
        risk_allowed=False,
    )

    TradePlanStep().execute(context)

    plan = context.execution["trade_plan"]

    assert plan["allowed"] is False
    assert plan["entry"] is None
    assert plan["stop"] is None
    assert plan["take_profit_1"] is None
    assert plan["take_profit_2"] is None
    assert plan["risk_reward"] == "NO TRADE"
    assert plan["position_size"] == 0.0
    assert plan["reason"] == "Risk was not approved"


def test_missing_risk_permission_is_rejected() -> None:
    context = build_context()
    del context.risk["allowed"]

    # Отказ сохранён; тип уточнён: отсутствующий ключ даёт None, что
    # является ошибкой ТИПА, а не значения. Проверяется главное — шаг
    # не строит план без подтверждённого разрешения риска.
    with pytest.raises(
        TypeError,
        match="risk allowed must be bool",
    ):
        TradePlanStep().execute(context)


def test_approved_risk_requires_buy_signal() -> None:
    context = build_context(
        signal="SELL",
        risk_allowed=True,
    )

    with pytest.raises(
        ValueError,
        match="approved risk requires BUY signal",
    ):
        TradePlanStep().execute(context)


def test_missing_market_price_is_rejected() -> None:
    context = build_context()
    del context.market["price"]

    with pytest.raises(
        ValueError,
        match="market price is missing or not a positive",
    ):
        TradePlanStep().execute(context)


def test_zero_atr_is_rejected() -> None:
    context = build_context(atr=0.0)

    with pytest.raises(
        ValueError,
        match="ATR must be greater than zero",
    ):
        TradePlanStep().execute(context)


def test_non_dictionary_trade_plan_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        TradePlan,
        "build",
        staticmethod(lambda **_: "invalid"),
    )

    with pytest.raises(
        TypeError,
        match=r"Trade plan must return dict",
    ):
        TradePlanStep().execute(context)


def test_missing_trade_plan_field_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        TradePlan,
        "build",
        staticmethod(
            lambda **_: {
                "entry": 100000.0,
                "stop": 99500.0,
                "take_profit_1": 101000.0,
                "risk_reward": "1:2 / 1:3",
            }
        ),
    )

    with pytest.raises(
        ValueError,
        match="TradePlan missing field: take_profit_2",
    ):
        TradePlanStep().execute(context)


def test_inconsistent_buy_levels_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        TradePlan,
        "build",
        staticmethod(
            lambda **_: {
                "entry": 100000.0,
                "stop": 100500.0,
                "take_profit_1": 101000.0,
                "take_profit_2": 101500.0,
                "risk_reward": "1:2 / 1:3",
            }
        ),
    )

    with pytest.raises(
        ValueError,
        match="BUY levels are inconsistent",
    ):
        TradePlanStep().execute(context)


def test_invalid_risk_reward_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        TradePlan,
        "build",
        staticmethod(
            lambda **_: {
                "entry": 100000.0,
                "stop": 99500.0,
                "take_profit_1": 101000.0,
                "take_profit_2": 101500.0,
                "risk_reward": "1:1",
            }
        ),
    )

    with pytest.raises(
        ValueError,
        match="risk reward must be 1:2 / 1:3",
    ):
        TradePlanStep().execute(context)