from api.contracts.context import MarketContext
from api.core.registry import ModuleRegistry


class CoreEngine:

    NAME = "Trading Core Engine"
    VERSION = "1.1.0"

    def __init__(self):
        self.registry = ModuleRegistry()

    def register(self, name: str, module):
        self.registry.register(name, module)

    def execute(self, context: MarketContext) -> MarketContext:

        for module in self.registry.all().values():
            context = module.process(context)

        return context