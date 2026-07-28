from abc import ABC, abstractmethod


class ExchangeBase(ABC):

    @abstractmethod
    def get_klines(self, symbol: str, interval: str, limit: int):
        """Получить свечи"""
        pass

    @abstractmethod
    def get_ticker(self, symbol: str):
        """Получить текущую цену"""
        pass

    @abstractmethod
    def get_orderbook(self, symbol: str):
        """Получить стакан"""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Проверка доступности API"""
        pass