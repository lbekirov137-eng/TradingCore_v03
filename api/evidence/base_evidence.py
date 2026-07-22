from abc import ABC, abstractmethod


class BaseEvidence(ABC):

    NAME = "Base Evidence"
    VERSION = "1.0.0"

    @abstractmethod
    def validate(self, candidate):
        """
        Проверяет новую торговую идею.

        Возвращает результат проверки.
        """
        pass