import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BinancePublicMarketProvider:
    """
    Loads public Binance Spot market data only.

    This module:
    - does not use API keys;
    - cannot create orders;
    - does not access balances;
    - returns only fully closed candles.
    """

    NAME = "Binance Public Market Provider"
    VERSION = "1.1.0"

    BASE_URL = "https://data-api.binance.vision"
    KLINES_PATH = "/api/v3/klines"

    DEFAULT_TIMEOUT_SECONDS = 15
    MIN_LIMIT = 2
    MAX_LIMIT = 1000

    SUPPORTED_INTERVALS = {
        "1s", "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    }

    @classmethod
    def fetch(
        cls,
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 250,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        symbol = cls._validate_symbol(symbol)
        cls._validate_interval(interval)
        cls._validate_limit(limit)
        cls._validate_timeout(timeout_seconds)

        request_limit = min(limit + 1, cls.MAX_LIMIT)
        query = urlencode({
            "symbol": symbol,
            "interval": interval,
            "limit": request_limit,
        })
        url = f"{cls.BASE_URL}{cls.KLINES_PATH}?{query}"

        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "TradingCore-PublicMarketProvider/1.1",
            },
            method="GET",
        )

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status_code = response.getcode()
                if status_code != 200:
                    raise RuntimeError(
                        "Binance market data request failed "
                        f"with HTTP status {status_code}"
                    )
                raw_body = response.read().decode("utf-8")
        except HTTPError as error:
            raise RuntimeError(
                "Binance market data HTTP error: "
                f"{error.code} {error.reason}"
            ) from error
        except URLError as error:
            raise RuntimeError(
                "Binance market data connection error: "
                f"{error.reason}"
            ) from error
        except TimeoutError as error:
            raise RuntimeError(
                "Binance market data request timed out"
            ) from error

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as error:
            raise RuntimeError("Binance returned invalid JSON") from error

        if not isinstance(payload, list):
            raise TypeError("Binance klines response must be a list")

        now_ms = int(time.time() * 1000)
        closed_rows = []

        for row in payload:
            cls._validate_kline_row(row)
            if int(row[6]) < now_ms:
                closed_rows.append(row)

        if len(closed_rows) < limit:
            raise RuntimeError(
                "Binance returned insufficient closed candles: "
                f"expected {limit}, got {len(closed_rows)}"
            )

        selected_rows = closed_rows[-limit:]

        open_times_ms = [int(row[0]) for row in selected_rows]
        close_times_ms = [int(row[6]) for row in selected_rows]
        opens = [float(row[1]) for row in selected_rows]
        highs = [float(row[2]) for row in selected_rows]
        lows = [float(row[3]) for row in selected_rows]
        closes = [float(row[4]) for row in selected_rows]
        base_volumes = [float(row[5]) for row in selected_rows]
        quote_volumes = [float(row[7]) for row in selected_rows]
        trade_counts = [int(row[8]) for row in selected_rows]

        latest_row = selected_rows[-1]

        return {
            "source": "BINANCE_PUBLIC_SPOT",
            "provider": cls.NAME,
            "provider_version": cls.VERSION,
            "is_live_data": True,
            "public_market_data_only": True,
            "api_key_used": False,
            "symbol": symbol,
            "interval": interval,
            "candles_count": len(selected_rows),
            "price": closes[-1],
            "open_times_ms": open_times_ms,
            "close_times_ms": close_times_ms,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volume": quote_volumes[-1],
            "volume_unit": "QUOTE_ASSET",
            "volumes": quote_volumes,
            "base_volumes": base_volumes,
            "quote_volumes": quote_volumes,
            "trade_counts": trade_counts,
            "base_volume": base_volumes[-1],
            "quote_volume": quote_volumes[-1],
            "last_open_time_ms": int(latest_row[0]),
            "last_close_time_ms": int(latest_row[6]),
            "last_trade_count": int(latest_row[8]),
        }

    @classmethod
    def _validate_symbol(cls, symbol: Any) -> str:
        if not isinstance(symbol, str):
            raise TypeError("Binance symbol must be str")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("Binance symbol must not be empty")
        if not normalized_symbol.isalnum():
            raise ValueError(
                "Binance symbol must contain only letters and numbers"
            )
        return normalized_symbol

    @classmethod
    def _validate_interval(cls, interval: Any) -> None:
        if not isinstance(interval, str):
            raise TypeError("Binance interval must be str")
        if interval not in cls.SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported Binance interval: {interval}")

    @classmethod
    def _validate_limit(cls, limit: Any) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("Binance kline limit must be int")
        if not cls.MIN_LIMIT <= limit < cls.MAX_LIMIT:
            raise ValueError(
                "Binance kline limit must be between 2 and 999"
            )

    @staticmethod
    def _validate_timeout(timeout_seconds: Any) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds,
            int,
        ):
            raise TypeError("Binance timeout must be int")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError(
                "Binance timeout must be between 1 and 60 seconds"
            )

    @staticmethod
    def _validate_kline_row(row: Any) -> None:
        if not isinstance(row, list):
            raise TypeError("Binance kline row must be a list")
        if len(row) < 12:
            raise ValueError(
                "Binance kline row must contain at least 12 fields"
            )
        try:
            int(row[0])
            float(row[1])
            float(row[2])
            float(row[3])
            float(row[4])
            float(row[5])
            int(row[6])
            float(row[7])
            int(row[8])
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Binance kline row contains invalid numeric values"
            ) from error
