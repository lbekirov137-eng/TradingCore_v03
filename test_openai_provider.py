from __future__ import annotations

import json
import sys
from uuid import uuid4

from ai_core.openai_provider import OpenAIProvider
from ai_core.provider_contracts import AIMode, AIRequest


def main() -> int:
    provider = OpenAIProvider()

    print("=" * 70)
    print("TRADINGCORE - OPENAI PROVIDER SAFE TEST")
    print("MODE: SHADOW")
    print("REAL ORDERS: DISABLED")
    print("=" * 70)

    if not provider.is_configured():
        print("RESULT: FAILED")
        print("REASON: OPENAI_API_KEY is not available")
        return 1

    request = AIRequest(
        task_type="paper_trade_review",
        request_id=f"test-{uuid4()}",
        mode=AIMode.SHADOW,
        max_cost_usd=0.02,
        timeout_seconds=20.0,
        payload={
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "candidate_side": "BUY",
            "strategy_signal": "BUY",
            "source_signal_score": 95,
            "market_regime": "UNKNOWN",
            "trend_1h": "DOWN",
            "trend_15m": "RANGE",
            "trend_5m": "UP",
            "structure": "UNKNOWN",
            "rsi": 71.4,
            "atr": 42.5,
            "risk_allowed": True,
            "risk_amount_usd": 1.0,
            "risk_reward": 3.0,
            "data_quality": "INCOMPLETE",
            "liquidity_quality": "UNKNOWN",
            "volatility_quality": "UNKNOWN",
            "real_orders_enabled": False,
        },
        metadata={
            "purpose": "safe_connectivity_test",
            "must_not_affect_trading": True,
        },
    )

    response = provider.analyze(request)

    print(json.dumps(
        response.to_dict(),
        ensure_ascii=False,
        indent=2,
    ))

    print("=" * 70)
    print("SAFETY CHECK")
    print("CAN OPEN TRADE:", response.can_open_trade)
    print("BLOCKS TRADE:", response.blocks_trade)
    print("SUCCESS:", response.success)
    print("=" * 70)

    if response.can_open_trade:
        print("RESULT: FAILED - unsafe provider behavior")
        return 2

    if not response.success:
        print("RESULT: FAILED - API or parsing error")
        return 3

    print("RESULT: PASSED")
    print("The AI response was produced in SHADOW mode.")
    print("No trading action was performed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
