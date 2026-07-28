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
    """
    Порядок регистрации = порядок ИСПОЛНЕНИЯ: CoreEngine.execute()
    итерирует registry.all().values(), то есть порядок функционально
    значим, а не косметичен.

    Ожидаемый список обновлён до фактического production-порядка.
    Авторитетным признан production-порядок, а не прежний список из
    6 шагов, на основании доказательств:

    1. Граф зависимостей: каждый потребитель идёт ПОСЛЕ поставщика
       (проверено по чтению/записи context в каждом шаге):
         indicator            market                 -> indicators
         market_intelligence  market, indicators     -> regime
         strategy             indicators             -> strategy
         vlad_orb_observer    market                 -> strategy
         strategy_coordinator strategy               -> strategy.selected_trade
         risk                 strategy.selected_trade, indicators, market, portfolio -> risk
         trade_plan           risk, strategy         -> execution.trade_plan
         decision             risk, execution, strategy -> decision
         paper_execution      decision, strategy     -> execution.paper_order
         coordinator_decision_journal  (только наблюдение) -> последний
    2. Рантайм: пайплайн проходит end-to-end и выдаёт
       FILLED_SIMULATED при real_order_sent = False.
    3. Этот же Bootstrap.build() используется живым эндпоинтом
       /analyze (api/analyzer.py).

    Прежний список из 6 шагов просто устарел: он предшествовал
    появлению market_intelligence, vlad_orb_observer,
    strategy_coordinator и coordinator_decision_journal.
    """
    engine = Bootstrap.build()

    modules = engine.registry.all()

    assert list(modules.keys()) == [
        "indicator",
        "market_intelligence",
        "strategy",
        "vlad_orb_observer",
        "strategy_coordinator",
        "risk",
        "trade_plan",
        "decision",
        "paper_execution",
        "coordinator_decision_journal",
    ]


def test_bootstrap_order_satisfies_safety_critical_dependencies() -> None:
    """
    Инварианты порядка, которые обязаны выполняться независимо от того,
    сколько шагов появится в будущем. В отличие от точного списка выше,
    эти проверки не устаревают при добавлении новых шагов и фиксируют
    ИМЕННО то, что важно для безопасности.
    """
    order = list(Bootstrap.build().registry.all().keys())

    def position(name: str) -> int:
        assert name in order, f"обязательный шаг отсутствует: {name}"
        return order.index(name)

    # Риск считается только после того, как выбран конкретный трейд.
    assert position("strategy_coordinator") < position("risk")

    # Торговый план строится только после расчёта риска.
    assert position("risk") < position("trade_plan")

    # Решение принимается только после построения плана.
    assert position("trade_plan") < position("decision")

    # КЛЮЧЕВОЕ: исполнение (даже бумажное) — только после решения.
    assert position("decision") < position("paper_execution")

    # Журнал наблюдения идёт последним и ни на что не влияет.
    assert position("coordinator_decision_journal") == len(order) - 1


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