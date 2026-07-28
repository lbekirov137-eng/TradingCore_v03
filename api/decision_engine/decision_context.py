from dataclasses import dataclass

from api.market_data.market_snapshot import MarketSnapshot


@dataclass
class DecisionContext:

    exchange: str

    symbol: str

    interval: str

    market: MarketSnapshot

    indicators: dict

    strategy_signals: list

    risk: dict

    timestamp: str