import pytest

from api.contracts.context import MarketContext
from api.core.engine import CoreEngine
from api.pipeline_v2.steps.base_step import BaseStep


class OrderedStep(BaseStep):
    def __init__(
        self,
        name: str,
        execution_order: list[str],
    ) -> None:
        self.name = name
        self.execution_order = execution_order

    def process(self, context: MarketContext) -> MarketContext:
        self.execution_order.append(self.name)
        return context


class ExecuteSpyStep(BaseStep):
    def __init__(self) -> None:
        self.execute_called = False
        self.process_called = False

    def execute(self, context: MarketContext) -> MarketContext:
        self.execute_called = True
        return super().execute(context)

    def process(self, context: MarketContext) -> MarketContext:
        self.process_called = True
        return context


class ReplacingStep(BaseStep):
    def __init__(self, replacement: MarketContext) -> None:
        self.replacement = replacement

    def process(self, context: MarketContext) -> MarketContext:
        return self.replacement


class CapturingStep(BaseStep):
    def __init__(self) -> None:
        self.received_context: MarketContext | None = None

    def process(self, context: MarketContext) -> MarketContext:
        self.received_context = context
        return context


def test_empty_engine_returns_same_context() -> None:
    engine = CoreEngine()
    context = MarketContext()

    result = engine.execute(context)

    assert result is context


def test_modules_execute_in_registration_order() -> None:
    engine = CoreEngine()
    context = MarketContext()
    execution_order: list[str] = []

    engine.register(
        "first",
        OrderedStep("first", execution_order),
    )
    engine.register(
        "second",
        OrderedStep("second", execution_order),
    )
    engine.register(
        "third",
        OrderedStep("third", execution_order),
    )

    engine.execute(context)

    assert execution_order == [
        "first",
        "second",
        "third",
    ]


def test_core_engine_calls_public_execute_method() -> None:
    engine = CoreEngine()
    context = MarketContext()
    module = ExecuteSpyStep()

    engine.register("spy", module)
    engine.execute(context)

    assert module.execute_called is True
    assert module.process_called is True


def test_updated_context_is_passed_to_next_module() -> None:
    engine = CoreEngine()

    original_context = MarketContext()
    replacement_context = MarketContext()
    capturing_step = CapturingStep()

    engine.register(
        "replacing",
        ReplacingStep(replacement_context),
    )
    engine.register(
        "capturing",
        capturing_step,
    )

    result = engine.execute(original_context)

    assert capturing_step.received_context is replacement_context
    assert result is replacement_context


def test_invalid_context_is_rejected() -> None:
    engine = CoreEngine()

    with pytest.raises(
        TypeError,
        match="CoreEngine expected MarketContext",
    ):
        engine.execute({})  # type: ignore[arg-type]