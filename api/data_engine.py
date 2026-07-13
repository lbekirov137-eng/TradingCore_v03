from api.binance import BinanceAPI


class DataEngine:

    @staticmethod
    def load(
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 300,
    ):

        candles = BinanceAPI.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        return {
            "opens": candles["opens"],
            "highs": candles["highs"],
            "lows": candles["lows"],
            "closes": candles["closes"],
            "volumes": candles["volumes"],
        }