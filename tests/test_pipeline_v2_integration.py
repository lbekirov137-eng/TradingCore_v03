import pytest

from api.contracts.context import MarketContext
from api.core.bootstrap import Bootstrap
from api.decision_engine.decision_engine import DecisionEngine
from dry_run import build_context


def build_market_context() -> MarketContext:
    """
    Единый реалистичный сетап ORB из dry_run.

    Здесь раньше лежала СВОЯ копия синтетического ряда
    (100 + i*0.2 + 2*sin(i*0.7)), страдавшая тем же дефектом, что и
    фикстура dry_run: монотонный рост уводил цену на ~4.7R от уровня
    входа, который vlad_orb берёт из свечи ретеста. Иными словами, тест
    проверял пайплайн на УСТАРЕВШЕМ сигнале и проходил только потому,
    что проверки актуальности входа тогда не существовало.

    Дублирование убрано: обе фикстуры описывали один и тот же сценарий и
    расходились бы при любой правке. Ассерты тестов не менялись.
    """
    return build_context()


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