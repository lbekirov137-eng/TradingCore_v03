"""
Прогон стратегии по историческим свечам с издержками.

ЧТО ЭТО НЕ ДЕЛАЕТ: не оптимизирует параметры, не перебирает варианты и не
выбирает лучший. Он исполняет ОДНУ зафиксированную конфигурацию и
возвращает сделки. Любой подбор значений здесь означал бы подгонку.

Правила исполнения повторяют PaperPositionManager, чтобы результат
бэктеста и paper-контура были сравнимы:
  - одна позиция за раз;
  - решения только по закрытым свечам;
  - если свеча задела и stop, и цель — считается STOP (консервативно);
  - издержки считаются один раз при закрытии, тем же cost_model.

Вход исполняется по закрытию сигнальной свечи — той самой, на которой
решение принято. Исполнение по открытию следующей свечи было бы честнее к
реальности, но потребовало бы данных, которых у стратегии в момент решения
нет; выбранный вариант согласован с paper-контуром, где вход тоже идёт по
цене последней закрытой свечи.
"""

from __future__ import annotations

from typing import Any, Sequence

from api.paper_trading.cost_model import TradingCostConfig, compute_trade_costs
from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    Candle,
    StrategyConfig,
)
from api.strategy_supervisor.stats import ClosedTrade


# Риск на сделку: 0.1% от 1000 (config/settings.py). Фиксирован — размер
# позиции выводится из него и из расстояния до стопа, а не подбирается.
RISK_AMOUNT_USD = 1.0

# Максимальная длительность сделки в барах. Нужен конечный горизонт, иначе
# незакрытая позиция висела бы до конца выборки и искажала статистику.
MAX_BARS_IN_TRADE = 288  # сутки на 5m


def _utc_iso(open_time_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(
        open_time_ms / 1000.0, tz=timezone.utc
    ).isoformat()


def _regime_for(atr_percent: float | None) -> str:
    """
    Грубая сегментация режима по волатильности.

    Границы взяты из config/adaptive_orb.py (ATR_LOW / ATR_HIGH), а не
    придуманы: они уже используются проектом для той же цели.
    """
    if atr_percent is None:
        return "UNKNOWN"

    if atr_percent < 0.8:
        return "RANGE"

    if atr_percent > 1.5:
        return "VOLATILE"

    return "TREND"


def run_backtest(
    strategy: BaseStrategy,
    candles: Sequence[Candle],
    cost_config: TradingCostConfig | None = None,
    max_bars_in_trade: int = MAX_BARS_IN_TRADE,
) -> dict[str, Any]:
    """
    Прогоняет стратегию по свечам и возвращает сделки и диагностику.

    Возвращает и число СИГНАЛОВ, и число ЗАКРЫТЫХ сделок: они расходятся,
    когда сигнал приходит при уже открытой позиции, и это расхождение
    нужно видеть — оно объясняет, почему сделок меньше, чем сетапов.
    """
    cost_config = cost_config or TradingCostConfig()

    trades: list[ClosedTrade] = []
    reason_counts: dict[str, int] = {}

    signals = 0
    suppressed_while_open = 0

    open_trade: dict[str, Any] | None = None

    for index in range(len(candles)):
        candle = candles[index]

        # --- сопровождение открытой позиции ---
        if open_trade is not None:
            stop = open_trade["stop"]
            target = open_trade["take_profit_2"]

            exit_price = None
            exit_reason = None

            # Консервативно: stop проверяется первым.
            if candle.low <= stop:
                exit_price = stop
                exit_reason = "STOP_LOSS"
            elif candle.high >= target:
                exit_price = target
                exit_reason = "TAKE_PROFIT_2"
            elif index - open_trade["entry_index"] >= max_bars_in_trade:
                exit_price = candle.close
                exit_reason = "TIME_STOP"

            if exit_price is not None:
                costs = compute_trade_costs(
                    entry_price=open_trade["entry"],
                    exit_price=exit_price,
                    quantity=open_trade["quantity"],
                    side="LONG",
                    config=cost_config,
                )

                risk = open_trade["risk_amount"]

                trades.append(
                    ClosedTrade(
                        strategy_id=strategy.strategy_key,
                        closed_at_utc=_utc_iso(candle.open_time_ms),
                        regime=open_trade["regime"],
                        net_pnl=costs["net_pnl"],
                        r_multiple=(
                            costs["net_pnl"] / risk if risk > 0 else None
                        ),
                    )
                )

                open_trade = None

            # Пока позиция открыта, новые сигналы не исполняются.
            if open_trade is not None:
                continue

        # --- поиск нового сетапа ---
        decision = strategy.evaluate_closed_candle(candles, index)

        reason_counts[decision.reason_code] = (
            reason_counts.get(decision.reason_code, 0) + 1
        )

        if not decision.is_trade:
            continue

        signals += 1

        if open_trade is not None:
            suppressed_while_open += 1
            continue

        entry = float(decision.entry)
        stop = float(decision.stop)

        risk_per_unit = entry - stop

        if risk_per_unit <= 0:
            continue

        quantity = RISK_AMOUNT_USD / risk_per_unit

        open_trade = {
            "entry": entry,
            "stop": stop,
            "take_profit_2": float(decision.take_profit_2),
            "quantity": quantity,
            "risk_amount": RISK_AMOUNT_USD,
            "entry_index": index,
            "regime": _regime_for(
                decision.diagnostics.get("atr_percent")
            ),
        }

    return {
        "strategy_key": strategy.strategy_key,
        "version": strategy.version,
        "parameter_fingerprint": strategy.config.fingerprint(),
        "candles": len(candles),
        "signals": signals,
        "closed_trades": len(trades),
        "suppressed_while_open": suppressed_while_open,
        "still_open_at_end": open_trade is not None,
        "trades": trades,
        "reason_counts": dict(
            sorted(reason_counts.items(), key=lambda pair: -pair[1])[:12]
        ),
        "cost_config": cost_config.snapshot(),
    }


def load_candles(path: str) -> list[Candle]:
    """Загружает свечи из проектного JSON. Файл только читается."""
    import json

    from api.strategy_engine.strategies.contracts import candles_from_arrays

    with open(path, "r", encoding="utf-8") as file:
        payload = json.load(file)

    return candles_from_arrays(payload)
