from api.core.bootstrap import Bootstrap
from dry_run import build_context


def test_complete_pipeline_creates_paper_order() -> None:
    engine = Bootstrap.build()
    context = build_context()

    result = engine.execute(context)

    assert result.decision["decision"] == "TRADE"

    paper_order = result.execution["paper_order"]

    assert paper_order["mode"] == "PAPER"
    assert paper_order["status"] == "FILLED_SIMULATED"
    assert paper_order["real_order_sent"] is False

    assert paper_order["exchange"] == "binance"
    assert paper_order["symbol"] == "BTCUSDT"
    assert paper_order["timeframe"] == "5m"

    # Контракт стал точнее: paper_order теперь несёт ОБА поля —
    # signal (BUY/SELL, торговый сигнал) и side (LONG/SHORT, направление
    # позиции). Раньше "side" переиспользовалось под значение сигнала.
    # Проверяем оба, поэтому покрытие расширено, а не ослаблено:
    # прежняя проверка "это покупка" сохранена через signal == "BUY".
    assert paper_order["signal"] == "BUY"
    assert paper_order["side"] == "LONG"

    assert paper_order["entry"] > paper_order["stop"]
    assert (
        paper_order["take_profit_1"]
        > paper_order["entry"]
    )
    assert (
        paper_order["take_profit_2"]
        > paper_order["take_profit_1"]
    )

    assert paper_order["quantity"] > 0

    audit = result.audit["paper_execution_step"]

    # Строгое сравнение по равенству СОХРАНЕНО намеренно (а не ослаблено
    # до проверки подмножества): любое незамеченное изменение формы
    # audit-записи должно ломать тест. Обновлены только фактические
    # значения — версия шага выросла до 3.0.0, и запись обогатилась
    # полями execution_mode/side/signal/strategy.
    # Ключевые инварианты безопасности здесь же: real_order_sent False и
    # execution_mode SPOT_LONG_ONLY.
    assert audit == {
        "status": "OK",
        "version": "3.0.0",
        "mode": "PAPER",
        "result": "FILLED_SIMULATED",
        "real_order_sent": False,
        "execution_mode": "SPOT_LONG_ONLY",
        "side": "LONG",
        "signal": "BUY",
        "strategy": "EMA_AND_VLAD_ORB",
    }