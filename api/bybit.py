import requests


class BybitAPI:

    BASE_URL = "https://api.bybit.com"

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

        response = requests.get(url, timeout=10)
        response.raise_for_status()

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