from api.contracts.context import MarketContext
from api.core.registry import ModuleRegistry
from api.pipeline_v2.steps.base_step import BaseStep


class CoreEngine:
    def __init__(self) -> None:
        self.registry = ModuleRegistry()

    def register(self, name: str, module: BaseStep) -> None:
        self.registry.register(name, module)

    def execute(self, context: MarketContext) -> MarketContext:
        if not isinstance(context, MarketContext):
            raise TypeError(
                f"CoreEngine expected MarketContext, "
                f"got {type(context).__name__}"
            )

        for module in self.registry.all().values():
            context = module.execute(context)

        return context