import pytest

from api.contracts.context import MarketContext
from api.contracts.selected_trade import side_for_signal
from api.pipeline_v2.steps.risk_step import RiskStep
from api.risk_engine import RiskEngine


def build_context(
    signal: str = "BUY",
    price: float = 100000.0,
    atr: float = 500.0,
    balance: float = 1000.0,
    risk_percent: float = 0.1,
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

    context.portfolio = {
        "balance": balance,
        "risk_percent": risk_percent,
    }

    return context


def test_buy_signal_is_approved() -> None:
    context = build_context()

    result = RiskStep().execute(context)

    assert result is context
    assert context.risk["allowed"] is True
    assert context.risk["risk_amount"] == 1.0
    assert context.risk["position_size"] == 0.002
    assert context.risk["stop_distance"] == 500.0
    assert context.risk["signal"] == "BUY"
    assert context.risk["execution_mode"] == "SPOT_LONG_ONLY"

    # Строгое сравнение по равенству СОХРАНЕНО: любое незамеченное
    # изменение формы audit-записи обязано ломать тест. Обновлены только
    # фактические значения — версия шага выросла до 4.0.0, формулировка
    # стала точнее ("by ATR" отличает ATR-путь от расчёта по стопу
    # стратегии), а запись обогатилась signal/side/strategy/execution_mode.
    # Инвариант безопасности здесь же: real_order_sent False.
    assert context.audit["risk_step"] == {
        "status": "OK",
        "version": "4.0.0",
        "allowed": True,
        "reason": "Risk approved by ATR",
        "signal": "BUY",
        "side": "LONG",
        "strategy": "EMA",
        "execution_mode": "SPOT_LONG_ONLY",
        "real_order_sent": False,
    }


def test_no_trade_signal_is_blocked() -> None:
    context = build_context(signal="NO TRADE")

    RiskStep().execute(context)

    assert context.risk["allowed"] is False
    assert context.risk["position_size"] == 0.0
    # Источник решения сместился на координатор; смысл тот же —
    # NO TRADE блокирует риск.
    assert context.risk["reason"] == "Coordinator returned NO TRADE"


def test_sell_signal_is_blocked_in_spot_long_only_mode() -> None:
    context = build_context(signal="SELL")

    RiskStep().execute(context)

    assert context.risk["allowed"] is False
    assert context.risk["position_size"] == 0.0
    assert (
        context.risk["reason"]
        == "SELL is disabled outside PAPER_LONG_SHORT mode"
    )


def test_default_portfolio_values_are_used() -> None:
    context = build_context()
    context.portfolio = {}

    RiskStep().execute(context)

    assert context.risk["balance"] == 1000.0
    assert context.risk["risk_percent"] == 0.1
    assert context.risk["risk_amount"] == 1.0


def test_missing_price_is_rejected() -> None:
    context = build_context()
    del context.market["price"]

    # Сообщение обновлено под текущий, БОЛЕЕ строгий код: RiskStep теперь
    # проверяет не только "> 0", но и конечность значения (NaN/Inf) через
    # _is_valid_number. Тип исключения (ValueError) и проверяемая семантика
    # (отсутствующая/непригодная цена отвергается) не изменились.
    with pytest.raises(
        ValueError,
        match="RiskStep price must be a positive finite number",
    ):
        RiskStep().execute(context)


def test_zero_atr_is_rejected() -> None:
    context = build_context(atr=0.0)

    # См. комментарий выше: проверка стала строже (покрывает NaN/Inf),
    # тип исключения и смысл теста прежние.
    with pytest.raises(
        ValueError,
        match="RiskStep ATR must be a positive finite number",
    ):
        RiskStep().execute(context)


def test_invalid_strategy_signal_is_rejected() -> None:
    context = build_context(signal="UNKNOWN")

    with pytest.raises(
        ValueError,
        match="RiskStep invalid selected signal: UNKNOWN",
    ):
        RiskStep().execute(context)


def test_excessive_risk_percent_is_rejected() -> None:
    context = build_context(risk_percent=0.2)

    with pytest.raises(
        ValueError,
        match=r"RiskStep risk percent must be within 0\.\.0\.1%",
    ):
        RiskStep().execute(context)


def test_non_dictionary_risk_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        RiskEngine,
        "calculate",
        staticmethod(lambda **_: "invalid"),
    )

    with pytest.raises(
        TypeError,
        match=r"RiskEngine result must be dict",
    ):
        RiskStep().execute(context)


def test_incomplete_risk_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context()

    monkeypatch.setattr(
        RiskEngine,
        "calculate",
        staticmethod(
            lambda **_: {
                "allowed": True,
                "risk_amount": 1.0,
            }
        ),
    )

    with pytest.raises(
        ValueError,
        match="RiskEngine field position_size must be finite",
    ):
        RiskStep().execute(context)
