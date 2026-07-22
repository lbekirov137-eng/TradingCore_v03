import json
import math

from api.contracts.context import MarketContext
from api.core.bootstrap import Bootstrap


def build_context() -> MarketContext:
    closes = [
        100.0
        + index * 0.2
        + 2.0 * math.sin(index * 0.7)
        for index in range(250)
    ]

    closes[-1] = closes[-2] + 0.3

    highs = [
        close + 1.0
        for close in closes
    ]

    lows = [
        close - 1.0
        for close in closes
    ]

    context = MarketContext()

    context.exchange = "binance"
    context.symbol = "BTCUSDT"
    context.timeframe = "5m"

    context.market = {
        "price": closes[-1],
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volume": 5000,
    }

    context.portfolio = {
        "balance": 1000.0,
        "risk_percent": 0.1,
    }

    # Безопасная подмена времени разрешена только для DRY_RUN.
    # 12 UTC находится внутри активной тестовой сессии.
    context.execution = {
        "runtime": {
            "mode": "DRY_RUN",
            "utc_hour_override": 12,
            "real_orders_enabled": False,
        },
    }

    return context


def main() -> None:
    print("=" * 60)
    print("TRADING CORE V2 - SAFE DRY RUN")
    print("REAL ORDERS: DISABLED")
    print("SIMULATED UTC HOUR: 12")
    print("=" * 60)

    engine = Bootstrap.build()
    context = build_context()

    result = engine.execute(context)

    report = {
        "mode": "DRY_RUN",
        "real_order_sent": False,
        "runtime": result.execution.get("runtime"),
        "exchange": result.exchange,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "market_price": result.market.get("price"),
        "indicators": result.indicators,
        "strategy": result.strategy,
        "risk": result.risk,
        "trade_plan": result.execution.get("trade_plan"),
        "decision": result.decision,
        "audit": result.audit,
    }

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    print("=" * 60)
    print("DRY RUN COMPLETED")
    print("NO REAL ORDER WAS SENT")
    print("=" * 60)


if __name__ == "__main__":
    main()