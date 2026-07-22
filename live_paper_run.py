import json
import sys
from typing import Any

from api.contracts.context import MarketContext
from api.core.bootstrap import Bootstrap
from api.providers.binance_public_market_provider import (
    BinancePublicMarketProvider,
)


SYMBOL = "BTCUSDT"
TIMEFRAME = "5m"
CANDLE_LIMIT = 250

PAPER_BALANCE = 1000.0
RISK_PERCENT = 0.1


def build_live_context() -> MarketContext:
    market_data = BinancePublicMarketProvider.fetch(
        symbol=SYMBOL,
        interval=TIMEFRAME,
        limit=CANDLE_LIMIT,
    )

    context = MarketContext()

    context.exchange = "binance"
    context.symbol = market_data["symbol"]
    context.timeframe = market_data["interval"]

    # Используются реальные публичные свечи Binance.
    context.market = market_data

    # Это виртуальный тестовый портфель.
    context.portfolio = {
        "balance": PAPER_BALANCE,
        "risk_percent": RISK_PERCENT,
    }

    # Реальные ордера принудительно отключены.
    # Подмена времени действует только внутри DRY_RUN.
    context.execution = {
        "runtime": {
            "mode": "DRY_RUN",
            "utc_hour_override": 12,
            "real_orders_enabled": False,
            "market_data_mode": "LIVE_PUBLIC",
        },
    }

    return context


def build_report(
    result: MarketContext,
) -> dict[str, Any]:
    return {
        "run_mode": "LIVE_DATA_PAPER_EXECUTION",
        "public_market_data_only": True,
        "api_key_used": result.market.get(
            "api_key_used",
            False,
        ),
        "real_orders_enabled": False,
        "real_order_sent": False,
        "exchange": result.exchange,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "market_source": result.market.get("source"),
        "candles_count": result.market.get(
            "candles_count"
        ),
        "market_price": result.market.get("price"),
        "last_close_time_ms": result.market.get(
            "last_close_time_ms"
        ),
        "strategy": result.strategy,
        "risk": result.risk,
        "trade_plan": result.execution.get(
            "trade_plan"
        ),
        "decision": result.decision,
        "paper_order": result.execution.get(
            "paper_order"
        ),
        "paper_execution_audit": result.audit.get(
            "paper_execution_step"
        ),
    }


def main() -> None:
    print("=" * 70)
    print("TRADING CORE V2 - LIVE MARKET DATA / PAPER EXECUTION")
    print("PUBLIC BINANCE DATA: ENABLED")
    print("API KEY: NOT USED")
    print("REAL ORDERS: DISABLED")
    print("=" * 70)

    try:
        context = build_live_context()

        engine = Bootstrap.build()
        result = engine.execute(context)

        report = build_report(result)

        print(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )

        paper_order = result.execution.get(
            "paper_order",
            {},
        )

        print("=" * 70)
        print(
            "FINAL DECISION:",
            result.decision.get("decision"),
        )
        print(
            "PAPER RESULT:",
            paper_order.get("status"),
        )
        print("LIVE PAPER RUN COMPLETED")
        print("NO REAL ORDER WAS SENT")
        print("=" * 70)

    except Exception as error:
        print("=" * 70)
        print("LIVE PAPER RUN FAILED SAFELY")
        print(f"ERROR TYPE: {type(error).__name__}")
        print(f"ERROR: {error}")
        print("NO REAL ORDER WAS SENT")
        print("=" * 70)

        sys.exit(1)


if __name__ == "__main__":
    main()