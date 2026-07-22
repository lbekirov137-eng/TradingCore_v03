import pytest

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep


class TrackingStep(BaseStep):
    def __init__(self) -> None:
        self.validation_called = False
        self.process_called = False

    def validate(self, context: MarketContext) -> None:
        self.validation_called = True
        super().validate(context)

    def process(self, context: MarketContext) -> MarketContext:
        self.process_called = True
        return context


class InvalidReturnStep(BaseStep):
    def process(self, context: MarketContext) -> MarketContext:
        return "invalid result"  # type: ignore[return-value]


def test_execute_calls_validation_and_process() -> None:
    context = MarketContext()
    step = TrackingStep()

    result = step.execute(context)

    assert step.validation_called is True
    assert step.process_called is True
    assert result is context


def test_invalid_context_is_rejected() -> None:
    step = TrackingStep()

    with pytest.raises(TypeError, match="expected MarketContext"):
        step.execute({})  # type: ignore[arg-type]

    assert step.validation_called is True
    assert step.process_called is False


def test_invalid_process_return_type_is_rejected() -> None:
    context = MarketContext()
    step = InvalidReturnStep()

    with pytest.raises(TypeError, match="must return MarketContext"):
        step.execute(context)