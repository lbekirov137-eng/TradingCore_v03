import math

import pytest

from api.contracts.context import MarketContext
from api.core.bootstrap import Bootstrap
from api.decision_engine.decision_engine import DecisionEngine


def build_market_context() -> MarketContext:
    closes = [
        100.0
        + index * 0.2
        + 2.0 * math.sin(index * 0.7)
        for index in range(250)
    ]

    # Последняя свеча должна подтвердить восходящую структуру.
    closes[-1] = closes[-2] + 0.3

    highs = [
        close + 1.0
        for close in closes
    ]

    lows = [
        close - 1.0
        for close in closes
    ]

    # opens/open_times_ms/interval добавлены потому, что VladORBObserverStep
    # передаёт context.market напрямую как market_snapshot, а vlad_orb
    # требует полный набор полей (interval, open_times_ms, opens, highs,
    # lows, closes) и одинаковую длину всех рядов.
    # Ряды строятся согласованно с уже существующими closes:
    #   - opens[i] = closes[i-1] (открытие свечи = закрытие предыдущей),
    #     opens[0] = closes[0];
    #   - open_times_ms — монотонная сетка с шагом 5 минут, что
    #     соответствует context.timeframe = "5m".
    # Assertions тестов не менялись — добавлены только недостающие
    # входные данные, которых требует продакшн-контракт.
    FIVE_MINUTES_MS = 5 * 60 * 1000
    BASE_OPEN_TIME_MS = 1_700_000_000_000

    opens = [closes[0]] + closes[:-1]

    open_times_ms = [
        BASE_OPEN_TIME_MS + index * FIVE_MINUTES_MS
        for index in range(len(closes))
    ]

    context = MarketContext()

    context.exchange = "binance"
    context.symbol = "BTCUSDT"
    context.timeframe = "5m"

    context.market = {
        "price": closes[-1],
        "interval": "5m",
        "open_times_ms": open_times_ms,
        "opens": opens,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volume": 5000,
    }

    context.portfolio = {
        "balance": 1000.0,
        "risk_percent": 0.1,
    }

    return context


def approve_decision(
    context: MarketContext,
) -> MarketContext:
    context.decision = {
        "decision": "TRADE",
        "score": 50,
        "confidence": 0.90,
        "failed_rules": [],
        "reason": "Integration test approval",
    }

    return context


def reject_decision(
    context: MarketContext,
) -> MarketContext:
    context.decision = {
        "decision": "NO_TRADE",
        "score": 20,
        "confidence": 0.80,
        "failed_rules": [
            "Integration Test Rule",
        ],
        "reason": "Integration test rejection",
    }

    return context


def test_complete_pipeline_produces_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(approve_decision),
    )

    engine = Bootstrap.build()
    context = build_market_context()

    result = engine.execute(context)

    assert result is context

    assert result.indicators["ema"]["trend"] == "BULLISH"

    assert (
        result.indicators["structure"]["structure"]
        == "UPTREND"
    )

    assert (
        0.0
        <= result.indicators["rsi"]["value"]
        < 70.0
    )

    assert result.strategy["signal"] == "BUY"

    assert result.risk["allowed"] is True
    assert result.risk["position_size"] > 0

    trade_plan = result.execution["trade_plan"]

    assert trade_plan["allowed"] is True
    assert trade_plan["signal"] == "BUY"
    assert trade_plan["entry"] > trade_plan["stop"]

    assert (
        trade_plan["take_profit_1"]
        > trade_plan["entry"]
    )

    assert (
        trade_plan["take_profit_2"]
        > trade_plan["take_profit_1"]
    )

    assert result.decision["engine_decision"] == "TRADE"
    assert result.decision["decision"] == "TRADE"

    assert result.audit["indicator_step"]["status"] == "OK"
    assert result.audit["strategy_step"]["status"] == "OK"
    assert result.audit["risk_step"]["status"] == "OK"
    assert result.audit["trade_plan_step"]["status"] == "OK"
    assert result.audit["decision_step"]["status"] == "OK"


def test_decision_engine_can_block_complete_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DecisionEngine,
        "process",
        staticmethod(reject_decision),
    )

    engine = Bootstrap.build()
    context = build_market_context()

    result = engine.execute(context)

    assert result.strategy["signal"] == "BUY"
    assert result.risk["allowed"] is True

    assert (
        result.execution["trade_plan"]["allowed"]
        is True
    )

    assert (
        result.decision["engine_decision"]
        == "NO_TRADE"
    )

    assert result.decision["decision"] == "NO_TRADE"

    assert (
        "DecisionEngine blocked trade: "
        "Integration test rejection"
        in result.decision["reason"]
    )