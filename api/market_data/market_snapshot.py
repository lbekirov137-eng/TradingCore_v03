from dataclasses import dataclass


@dataclass
class MarketSnapshot:

    exchange: str

    symbol: str

    interval: str

    timestamps: list

    opens: list

    highs: list

    lows: list

    closes: list

    volumes: list

    server_time: int = 0

    latency: float = 0.0