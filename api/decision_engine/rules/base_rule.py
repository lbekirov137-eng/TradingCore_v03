from abc import ABC, abstractmethod

from api.contracts.context import MarketContext


class BaseRule(ABC):

    NAME = "Base Rule"
    VERSION = "1.0.0"

    @abstractmethod
    def evaluate(self, context: MarketContext) -> dict:
        """
        Выполняет проверку одного правила.
        Должен вернуть словарь с результатом проверки.
        """
        pass