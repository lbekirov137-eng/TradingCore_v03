from api.binance import BinanceAPI
from api.bybit import BybitAPI

from config.settings import (
    DEFAULT_DATA_EXCHANGE,
    AUTO_SELECT_EXCHANGE,
)


class ExchangeRouter:

    PROVIDERS = {
        "binance": BinanceAPI,
        "bybit": BybitAPI,
    }

    @staticmethod
    def get_available_exchanges():

        available = []

        for name, provider in ExchangeRouter.PROVIDERS.items():

            try:
                provider.get_klines(limit=1)
                available.append(name)

            except Exception:
                continue

        return available

    @staticmethod
    def choose_exchange():

        if not AUTO_SELECT_EXCHANGE:
            return DEFAULT_DATA_EXCHANGE

        available = ExchangeRouter.get_available_exchanges()

        if not available:
            raise RuntimeError("Нет доступных бирж.")

        if DEFAULT_DATA_EXCHANGE in available:
            return DEFAULT_DATA_EXCHANGE

        return available[0]