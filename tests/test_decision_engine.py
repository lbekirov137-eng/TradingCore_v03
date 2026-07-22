from api.contracts.context import MarketContext
from api.decision_engine.decision_engine import DecisionEngine


def test_decision_engine():

    context = MarketContext()

    context.market = {
        "volume": 5000,
    }

    context.indicators = {
        "ema": {
            "trend": "BULLISH",
        }
    }

    context.risk = {
        "allowed": True,
    }

    context = DecisionEngine.process(context)

    print("\n===== DECISION ENGINE TEST =====")
    print(context.decision)
    print("================================")


if __name__ == "__main__":
    test_decision_engine()