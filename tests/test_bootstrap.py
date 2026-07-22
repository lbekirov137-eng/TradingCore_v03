from api.core.bootstrap import Bootstrap
from api.core.engine import CoreEngine
from api.pipeline_v2.steps.decision_step import DecisionStep
from api.pipeline_v2.steps.indicator_step import IndicatorStep
from api.pipeline_v2.steps.paper_execution_step import (
    PaperExecutionStep,
)
from api.pipeline_v2.steps.risk_step import RiskStep
from api.pipeline_v2.steps.strategy_step import StrategyStep
from api.pipeline_v2.steps.trade_plan_step import TradePlanStep


def test_bootstrap_returns_core_engine() -> None:
    engine = Bootstrap.build()

    assert isinstance(engine, CoreEngine)


def test_bootstrap_registers_expected_modules_in_order() -> None:
    engine = Bootstrap.build()

    modules = engine.registry.all()

    assert list(modules.keys()) == [
        "indicator",
        "strategy",
        "risk",
        "trade_plan",
        "decision",
        "paper_execution",
    ]


def test_bootstrap_registers_correct_module_types() -> None:
    engine = Bootstrap.build()

    modules = engine.registry.all()

    assert isinstance(
        modules["indicator"],
        IndicatorStep,
    )
    assert isinstance(
        modules["strategy"],
        StrategyStep,
    )
    assert isinstance(
        modules["risk"],
        RiskStep,
    )
    assert isinstance(
        modules["trade_plan"],
        TradePlanStep,
    )
    assert isinstance(
        modules["decision"],
        DecisionStep,
    )
    assert isinstance(
        modules["paper_execution"],
        PaperExecutionStep,
    )


def test_bootstrap_builds_independent_engines() -> None:
    first_engine = Bootstrap.build()
    second_engine = Bootstrap.build()

    assert first_engine is not second_engine
    assert first_engine.registry is not second_engine.registry

    first_modules = first_engine.registry.all()
    second_modules = second_engine.registry.all()

    assert list(first_modules.keys()) == list(
        second_modules.keys()
    )

    for name in first_modules:
        assert first_modules[name] is not second_modules[name]