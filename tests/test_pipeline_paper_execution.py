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
    assert paper_order["side"] == "BUY"

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

    assert audit == {
        "status": "OK",
        "version": "1.0.0",
        "mode": "PAPER",
        "result": "FILLED_SIMULATED",
        "real_order_sent": False,
    }