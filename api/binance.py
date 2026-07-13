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

        opens = []
        highs = []
        lows = []
        closes = []
        volumes = []

        for candle in data:
            opens.append(float(candle[1]))
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
            closes.append(float(candle[4]))
            volumes.append(float(candle[5]))

        return {
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": volumes,
        }