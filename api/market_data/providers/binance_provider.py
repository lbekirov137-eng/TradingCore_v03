from api.binance import BinanceAPI

from api.market_data.exchange_base import ExchangeBase


class BinanceProvider(ExchangeBase):

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ):
        return BinanceAPI.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

    def get_ticker(self, symbol: str):
        return BinanceAPI.get_ticker(symbol)

    def get_orderbook(self, symbol: str):
        return BinanceAPI.get_orderbook(symbol)

    def health_check(self):

        try:
            BinanceAPI.get_klines(limit=1)
            return True

        except Exception:
            return False