from abc import ABC, abstractmethod


class BaseProvider(ABC):

    NAME = "Base Provider"
    VERSION = "1.0.0"

    @abstractmethod
    def fetch(self):
        """
        Получить данные от внешнего источника.
        """
        pass