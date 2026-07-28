import time

from api.market_data.market_cache import MarketCache

from api.market_data.providers.binance_provider import BinanceProvider
from api.market_data.providers.bybit_provider import BybitProvider
from api.market_data.candle_utils import drop_unclosed_candle, validate_candles


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

        candles = provider.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        # Биржа отдаёт последней ещё не закрытую свечу — отбрасываем её,
        # иначе стратегии торгуют по "живой", постоянно меняющейся цене.
        candles = drop_unclosed_candle(
            candles,
            interval,
            now_ms=int(time.time() * 1000),
        )

        validate_candles(candles, interval)

        return candles