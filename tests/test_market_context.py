from api.contracts.context import MarketContext


def test_market_context_has_safe_default_values() -> None:
    context = MarketContext()

    assert context.symbol == ""
    assert context.exchange == ""
    assert context.timeframe == ""

    assert context.market == {}
    assert context.indicators == {}
    assert context.regime == {}
    assert context.strategy == {}
    assert context.risk == {}
    assert context.rules == {}
    assert context.decision == {}
    assert context.execution == {}
    assert context.portfolio == {}
    assert context.audit == {}


def test_market_context_accepts_identity_fields() -> None:
    context = MarketContext(
        symbol="BTC/USDT",
        exchange="binance",
        timeframe="5m",
    )

    assert context.symbol == "BTC/USDT"
    assert context.exchange == "binance"
    assert context.timeframe == "5m"


def test_context_sections_are_independent() -> None:
    context = MarketContext()

    context.market["price"] = 100000.0
    context.indicators["ema"] = {"trend": "BULLISH"}

    assert context.market == {
        "price": 100000.0,
    }
    assert context.indicators == {
        "ema": {
            "trend": "BULLISH",
        }
    }

    assert context.strategy == {}
    assert context.risk == {}
    assert context.decision == {}


def test_different_context_instances_do_not_share_data() -> None:
    first_context = MarketContext()
    second_context = MarketContext()

    first_context.market["price"] = 100000.0
    first_context.risk["approved"] = True
    first_context.audit["module"] = "RiskStep"

    assert second_context.market == {}
    assert second_context.risk == {}
    assert second_context.audit == {}

    assert first_context.market is not second_context.market
    assert first_context.risk is not second_context.risk
    assert first_context.audit is not second_context.audit