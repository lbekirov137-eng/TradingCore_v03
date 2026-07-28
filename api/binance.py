import requests


class BinanceAPI:

    BASE_URL = "https://api.binance.com"

    @staticmethod
    def get_klines(
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 300,
    ):

        url = (
            f"{BinanceAPI.BASE_URL}/api/v3/klines"
            f"?symbol={symbol}"
            f"&interval={interval}"
            f"&limit={limit}"
        )

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()

        timestamps = []
        opens = []
        highs = []
        lows = []
        closes = []
        volumes = []

        for candle in data:

            timestamps.append(int(candle[0]))

            opens.append(float(candle[1]))
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
            closes.append(float(candle[4]))
            volumes.append(float(candle[5]))

        return {
            "timestamps": timestamps,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": volumes,
        }

    @staticmethod
    def get_ticker(symbol: str = "BTCUSDT"):

        url = f"{BinanceAPI.BASE_URL}/api/v3/ticker/price?symbol={symbol}"

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()

        return {
            "symbol": data["symbol"],
            "price": float(data["price"]),
        }

    @staticmethod
    def get_orderbook(symbol: str = "BTCUSDT", limit: int = 20):

        url = (
            f"{BinanceAPI.BASE_URL}/api/v3/depth"
            f"?symbol={symbol}&limit={limit}"
        )

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()

        return {
            "bids": [[float(p), float(q)] for p, q in data["bids"]],
            "asks": [[float(p), float(q)] for p, q in data["asks"]],
        }