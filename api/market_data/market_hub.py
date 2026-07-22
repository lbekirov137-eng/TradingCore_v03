from api.market_data.market_cache import MarketCache

from api.market_data.providers.binance_provider import BinanceProvider
from api.market_data.providers.bybit_provider import BybitProvider


class MarketHub:

    def __init__(self):

        self.cache = MarketCache()

        self.providers = {
            "binance": BinanceProvider(),
            "bybit": BybitProvider(),
        }

    def get_provider(self, exchange):

        if exchange not in self.providers:
            raise ValueError(f"Unknown exchange: {exchange}")

        return self.providers[exchange]

    def get_klines(
        self,
        exchange,
        symbol,
        interval,
        limit,
    ):

        provider = self.get_provider(exchange)

        return provider.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )