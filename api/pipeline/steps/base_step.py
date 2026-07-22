from abc import ABC, abstractmethod
from api.contracts.context import MarketContext


class BaseStep(ABC):

    NAME = "Base Step"

    VERSION = "1.0.0"

    @abstractmethod
    def process(self, context: MarketContext) -> MarketContext:
        pass