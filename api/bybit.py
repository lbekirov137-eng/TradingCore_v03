import requests

from api.market_data.resilience import retry_with_backoff


class BybitAPI:

    BASE_URL = "https://api.bybit.com"

    @staticmethod
    def get_server_time() -> int:
        url = f"{BybitAPI.BASE_URL}/v5/market/time"

        def _call():
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response

        response = retry_with_backoff(_call)
        result = response.json()["result"]
        return int(result["timeSecond"]) * 1000

    @staticmethod
    def get_klines(
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 300,
    ):

        interval_map = {
            "1m": "1",
            "3m": "3",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "4h": "240",
            "1d": "D",
        }

        bybit_interval = interval_map.get(interval, "5")

        url = (
            f"{BybitAPI.BASE_URL}/v5/market/kline"
            f"?category=linear"
            f"&symbol={symbol}"
            f"&interval={bybit_interval}"
            f"&limit={limit}"
        )

        def _call():
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response

        response = retry_with_backoff(_call)

        data = response.json()["result"]["list"]

        # Bybit возвращает свечи от новых к старым
        data.reverse()

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

        url = (
            f"{BybitAPI.BASE_URL}/v5/market/tickers"
            f"?category=linear&symbol={symbol}"
        )

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()["result"]["list"][0]

        return {
            "symbol": data["symbol"],
            "price": float(data["lastPrice"]),
        }

    @staticmethod
    def get_orderbook(symbol: str = "BTCUSDT", limit: int = 20):

        url = (
            f"{BybitAPI.BASE_URL}/v5/market/orderbook"
            f"?category=linear&symbol={symbol}&limit={limit}"
        )

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()["result"]

        return {
            "bids": [[float(p), float(q)] for p, q in data["b"]],
            "asks": [[float(p), float(q)] for p, q in data["a"]],
        }