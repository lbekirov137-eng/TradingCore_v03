import pytest

from api.contracts.context import MarketContext
from api.core.registry import ModuleRegistry
from api.pipeline_v2.steps.base_step import BaseStep


class DummyStep(BaseStep):
    def process(self, context: MarketContext) -> MarketContext:
        return context


def test_register_and_get_module() -> None:
    registry = ModuleRegistry()
    module = DummyStep()

    registry.register("dummy", module)

    assert registry.exists("dummy") is True
    assert registry.get("dummy") is module


def test_unknown_module_returns_none() -> None:
    registry = ModuleRegistry()

    assert registry.exists("unknown") is False
    assert registry.get("unknown") is None


def test_empty_module_name_is_rejected() -> None:
    registry = ModuleRegistry()
    module = DummyStep()

    with pytest.raises(
        ValueError,
        match="Module name must be a non-empty string",
    ):
        registry.register("   ", module)


def test_invalid_module_type_is_rejected() -> None:
    registry = ModuleRegistry()

    with pytest.raises(
        TypeError,
        match="Registered module must inherit from BaseStep",
    ):
        registry.register("invalid", object())  # type: ignore[arg-type]


def test_duplicate_module_name_is_rejected() -> None:
    registry = ModuleRegistry()

    registry.register("dummy", DummyStep())

    with pytest.raises(
        ValueError,
        match="already registered",
    ):
        registry.register("dummy", DummyStep())


def test_registration_order_is_preserved() -> None:
    registry = ModuleRegistry()

    first = DummyStep()
    second = DummyStep()
    third = DummyStep()

    registry.register("first", first)
    registry.register("second", second)
    registry.register("third", third)

    modules = registry.all()

    assert list(modules.keys()) == [
        "first",
        "second",
        "third",
    ]


def test_all_returns_copy_of_registry() -> None:
    registry = ModuleRegistry()
    module = DummyStep()

    registry.register("dummy", module)

    external_modules = registry.all()
    external_modules.clear()

    assert registry.exists("dummy") is True
    assert registry.get("dummy") is module