import requests


class MarketData:

    BASE_URL = "https://api.bybit.com"

    @staticmethod
    def get_ticker(symbol="BTCUSDT"):

        url = f"{MarketData.BASE_URL}/v5/market/tickers"

        params = {
            "category": "linear",
            "symbol": symbol
        }

        response = requests.get(url, params=params, timeout=10)

        response.raise_for_status()

        return response.json()