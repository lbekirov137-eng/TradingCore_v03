from api.bybit import BybitAPI

from api.market_data.exchange_base import ExchangeBase


class BybitProvider(ExchangeBase):

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ):
        return BybitAPI.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

    def get_ticker(self, symbol: str):
        return BybitAPI.get_ticker(symbol)

    def get_orderbook(self, symbol: str):
        return BybitAPI.get_orderbook(symbol)

    def health_check(self):

        try:
            BybitAPI.get_klines(limit=1)
            return True

        except Exception:
            return False