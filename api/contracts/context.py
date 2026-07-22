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

    # Все результаты Rule-модулей
    rules: dict[str, Any] = field(default_factory=dict)

    # Итоговое решение Decision Engine
    decision: dict[str, Any] = field(default_factory=dict)

    execution: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)