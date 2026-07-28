from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketContext:

    symbol: str = ""
    exchange: str = ""
    timeframe: str = ""

    market: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, Any] = field(default_factory=dict)
    regime: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class LiveContext:
    """
    Контекст одного тика Scheduler для paper/live режима.

    В отличие от BacktestContext, visible_market всегда равен market:
    в реальном времени нет "будущих" свечей, которые нужно скрывать.
    visible_market — свойство (а не отдельное поле), чтобы не было
    риска рассинхронизации между ними.
    """

    exchange: str
    symbol: str
    interval: str
    limit: int = 300

    market: Any = None
    indicators: dict = field(default_factory=dict)
    strategy_signals: list = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)

    # Переопределяет «сейчас» для проверки свежести данных. None означает
    # реальное время (стенные часы) — единственный корректный режим для
    # живой торговли. Задаётся только для детерминированного replay.
    now_ms: Any = None

    # Явный признак воспроизведения исторических данных. Должен быть False
    # в любом реальном (paper-forward/demo) запуске, иначе проверка
    # устаревания данных перестанет защищать от stale-фида.
    replay_mode: bool = False

    @property
    def visible_market(self):
        return self.market