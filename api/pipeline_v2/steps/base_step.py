from abc import ABC, abstractmethod

from api.contracts.context import MarketContext


class BaseStep(ABC):

    NAME = "Base Step"
    VERSION = "2.0.0"

    @abstractmethod
    def process(self, context: MarketContext) -> MarketContext:
        """
        Выполняет один этап обработки MarketContext.
        Каждый Step получает Context,
        изменяет его при необходимости
        и возвращает обратно.
        """
        pass