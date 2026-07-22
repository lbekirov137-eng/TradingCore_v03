from api.market_data.market_hub import MarketHub
from api.market_data.market_snapshot import MarketSnapshot


class DataEngine:

    @staticmethod
    def load(
        exchange: str = "binance",
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 300,
    ):

        hub = MarketHub()

        candles = hub.get_klines(
            exchange=exchange,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        return MarketSnapshot(
            exchange=exchange,
            symbol=symbol,
            interval=interval,
            timestamps=candles["timestamps"],
            opens=candles["opens"],
            highs=candles["highs"],
            lows=candles["lows"],
            closes=candles["closes"],
            volumes=candles["volumes"],
            server_time=0,
            latency=0.0,
        )