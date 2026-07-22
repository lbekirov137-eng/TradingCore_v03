from dataclasses import dataclass


@dataclass
class BacktestContext:

    index: int
    market: object
    indicators: dict
    balance: float
    position: object = None

    @property
    def visible_market(self):

        m = self.market
        i = self.index + 1

        class VisibleMarket:
            timestamps = m.timestamps[:i]
            opens = m.opens[:i]
            highs = m.highs[:i]
            lows = m.lows[:i]
            closes = m.closes[:i]
            volumes = m.volumes[:i]

        return VisibleMarket()