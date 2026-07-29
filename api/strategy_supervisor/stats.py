"""
Статистика закрытых paper-сделок для решений супервизора.

ЗАЧЕМ ОТДЕЛЬНО ОТ paper_analytics. Отчёт в paper_analytics отвечает на
вопрос «что происходило». Здесь считается то, на что супервизор ОПИРАЕТСЯ
при остановке стратегии, и требования жёстче:

  - expectancy и R-мультипликаторы обязательны (по деньгам сравнивать
    стратегии нельзя: разный размер позиции даст разный ответ на
    одинаковом поведении);
  - нужно покрытие выборки — сколько торговых дней и сколько режимов,
    потому что 50 сделок одного дня в одном режиме не являются выборкой;
  - нужна скользящая оценка последних N сделок, чтобы деградацию было
    видно раньше, чем её размоет общая история.

Все метрики считаются ТОЛЬКО по закрытым сделкам и ТОЛЬКО после издержек.
Незакрытая позиция результата ещё не имеет, и учитывать её означало бы
подмешивать в статистику нереализованную переоценку.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


# Размер скользящего окна. 20 — компромисс: достаточно, чтобы одна сделка
# не переворачивала знак, и достаточно мало, чтобы деградация проявилась
# раньше, чем её усреднит вся история.
ROLLING_WINDOW = 20


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    number = float(value)

    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class ClosedTrade:
    """
    Одна закрытая paper-сделка, приведённая к сравнимому виду.

    r_multiple — результат в единицах риска ПОСЛЕ издержек. Именно он, а
    не деньги, позволяет сравнивать стратегии между собой.
    """

    strategy_id: str | None
    closed_at_utc: str | None
    regime: str | None
    net_pnl: float
    r_multiple: float | None

    @property
    def utc_date(self) -> str | None:
        if not isinstance(self.closed_at_utc, str) or len(self.closed_at_utc) < 10:
            return None

        return self.closed_at_utc[:10]


def closed_trade_from_observation(
    observation: dict[str, Any],
) -> ClosedTrade | None:
    """
    Строит сделку из наблюдения paper_analytics.

    Возвращает None, если запись не является закрытой сделкой или не
    содержит фактического результата. Отбрасывание здесь безопаснее
    подстановки нуля: ноль стал бы «безубыточной сделкой» и разбавил бы
    expectancy.
    """
    if not isinstance(observation, dict):
        return None

    if observation.get("position_event") != "POSITION_CLOSED":
        return None

    net_pnl = _finite(observation.get("net_pnl"))

    if net_pnl is None:
        net_pnl = _finite(observation.get("realized_pnl"))

    if net_pnl is None:
        return None

    risk = _finite(observation.get("risk_amount"))

    if risk is None or risk <= 0:
        entry = _finite(observation.get("entry"))
        stop = _finite(observation.get("stop"))
        quantity = _finite(observation.get("quantity"))

        if None not in (entry, stop, quantity):
            risk = abs(entry - stop) * quantity

    r_multiple = (net_pnl / risk) if risk and risk > 0 else None

    return ClosedTrade(
        strategy_id=observation.get("strategy_id"),
        closed_at_utc=observation.get("recorded_at_utc"),
        regime=observation.get("market_regime"),
        net_pnl=net_pnl,
        r_multiple=r_multiple,
    )


def closed_trades_from_observations(
    observations: Iterable[dict[str, Any]],
) -> list[ClosedTrade]:
    trades = (
        closed_trade_from_observation(observation)
        for observation in observations
    )

    return [trade for trade in trades if trade is not None]


def _expectancy(r_multiples: Sequence[float]) -> float | None:
    """
    Математическое ожидание в R на сделку.

    Считается по R, а не по деньгам: expectancy в долларах зависит от
    размера позиции и не сравним между стратегиями.
    """
    if not r_multiples:
        return None

    return sum(r_multiples) / len(r_multiples)


def _max_drawdown_r(r_multiples: Sequence[float]) -> float | None:
    """
    Просадка кривой в единицах R, положительной величиной.

    В R, а не в деньгах и не в процентах: лимит просадки должен означать
    одно и то же независимо от капитала и от того, менялся ли он.
    """
    if not r_multiples:
        return None

    equity = 0.0
    peak = 0.0
    drawdown = 0.0

    for value in r_multiples:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    return drawdown


def _max_loss_streak(net_pnls: Sequence[float]) -> int:
    streak = 0
    longest = 0

    for pnl in net_pnls:
        if pnl < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    return longest


def build_stats(
    trades: Sequence[ClosedTrade],
    rolling_window: int = ROLLING_WINDOW,
) -> dict[str, Any]:
    """
    Сводит закрытые сделки в метрики, на которых принимаются решения.

    Метрика без данных — None. Ни одна метрика не подменяется нулём:
    profit_factor=0.0 читался бы как «катастрофа», хотя означал бы всего
    лишь «сделок не было».
    """
    trades = list(trades)

    net_pnls = [trade.net_pnl for trade in trades]
    r_multiples = [
        trade.r_multiple
        for trade in trades
        if trade.r_multiple is not None
    ]

    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    profit_factor: float | None = None

    if net_pnls and gross_loss > 0:
        profit_factor = gross_profit / gross_loss

    rolling = r_multiples[-rolling_window:] if r_multiples else []

    trading_days = sorted(
        {trade.utc_date for trade in trades if trade.utc_date is not None}
    )
    regimes = sorted(
        {
            trade.regime.strip().upper()
            for trade in trades
            if isinstance(trade.regime, str) and trade.regime.strip()
        }
    )

    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": (
            round(len(wins) / len(trades) * 100.0, 2) if trades else None
        ),
        "net_pnl": round(sum(net_pnls), 8) if net_pnls else None,
        "profit_factor": (
            round(profit_factor, 3) if profit_factor is not None else None
        ),
        "expectancy_r": (
            round(_expectancy(r_multiples), 4)
            if r_multiples
            else None
        ),
        "average_r": (
            round(sum(r_multiples) / len(r_multiples), 4)
            if r_multiples
            else None
        ),
        "max_drawdown_r": (
            round(_max_drawdown_r(r_multiples), 4)
            if r_multiples
            else None
        ),
        "max_loss_streak": _max_loss_streak(net_pnls),
        "rolling_window": rolling_window,
        "rolling_trades": len(rolling),
        "rolling_expectancy_r": (
            round(_expectancy(rolling), 4)
            if len(rolling) >= rolling_window
            else None
        ),
        "trading_days": len(trading_days),
        "trading_day_list": trading_days,
        "regimes_observed": regimes,
        "regime_count": len(regimes),
    }


def build_stats_from_observations(
    observations: Iterable[dict[str, Any]],
    rolling_window: int = ROLLING_WINDOW,
) -> dict[str, Any]:
    return build_stats(
        closed_trades_from_observations(observations),
        rolling_window=rolling_window,
    )
