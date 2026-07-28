from dataclasses import dataclass


@dataclass
class BacktestContext:

    index: int
    market: object
    indicators: dict
    balance: float
    position: object = None

    @property
    def now_ms(self):
        """
        «Сейчас» в режиме воспроизведения — время последней ВИДИМОЙ свечи.
        Благодаря этому проверка устаревания данных не считает исторические
        данные протухшими во время бэктеста, но остаётся строгой в реальном
        времени (см. filters/regime.py::resolve_now).
        """
        return self.market.timestamps[self.index]

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