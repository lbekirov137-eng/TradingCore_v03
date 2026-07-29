"""
Аудит структуры издержек. Отвечает на вопрос «сколько R съедает вход и
выход» ДО того, как что-либо менять в стратегиях.

КЛЮЧЕВОЕ ТОЖДЕСТВО, из которого следует всё остальное:

    quantity  = risk / stop_distance
    notional  = quantity * price = risk * price / stop_distance
    cost      = notional * cost_rate
    cost_in_R = cost / risk = cost_rate / (stop_distance / price)

То есть cost_in_R = cost_rate / stop_percent.

Из этого видно главное: издержки в R НЕ зависят ни от риска, ни от размера
счёта, ни от плеча. Они зависят ТОЛЬКО от ставки издержек и от ширины стопа
в процентах цены. Поэтому «уменьшить риск» издержки не лечит, а «расширить
стоп» лечит линейно. Это же объясняет, почему 0.1% риска на 5m с
1-ATR стопом даёт около 1.9R: стоп там ~0.16% цены, а round-trip ~0.30%.

Все ставки берутся из config (api/paper_trading/cost_model.py). Значения,
которых в проекте нет (спред), помечены как ASSUMPTION и вынесены
отдельным полем — смешивать их с настроенными тарифами нельзя.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from api.paper_trading.cost_model import TradingCostConfig


# ---------------------------------------------------------------- профили

TAKER_TAKER = "TAKER_TAKER"
MAKER_TAKER = "MAKER_TAKER"


# Спред в проекте НЕ задан. Значение введено здесь как явное допущение
# исполнения, а не как тариф биржи. 1 bp для BTCUSDT спот — осторожная,
# но не абсурдная оценка на ликвидной паре.
ASSUMED_SPREAD_BPS = 1.0

# Минимальный номинал ордера. У Binance для BTCUSDT спот это 10 USDT
# (MIN_NOTIONAL). Проверяется, потому что при узком стопе и риске 0.1%
# от 1000 USD номинал может оказаться слишком МАЛЕНЬКИМ только если стоп
# очень широкий — обратная сторона той же арифметики.
MIN_NOTIONAL_USDT = 10.0

BASIS_POINT = 1.0 / 10_000.0


@dataclass(frozen=True)
class CostScenario:
    """Один сценарий: таймфрейм, ширина стопа, профиль исполнения."""

    name: str
    timeframe: str
    stop_label: str
    stop_percent: float          # ширина стопа в % от цены
    execution_profile: str

    @property
    def stop_fraction(self) -> float:
        return self.stop_percent / 100.0


def execution_rates(
    profile: str,
    config: TradingCostConfig,
    spread_bps: float = ASSUMED_SPREAD_BPS,
) -> dict[str, float]:
    """
    Раскладка ставок round-trip по профилю исполнения.

    Разделение принципиально: тарифы биржи настраиваются, а допущения об
    исполнении (проскальзывание, спред) — это модель, и они не должны
    выглядеть как измеренные величины.

    MAKER_TAKER: лимитный вход исполняется по своей цене, поэтому
    проскальзывания на входе НЕТ. Половина спреда на входе тоже не
    платится — мейкер его получает, а не платит. Выход остаётся рыночным.
    """
    spread_rate = spread_bps * BASIS_POINT
    slippage_rate = config.slippage_rate

    if profile == MAKER_TAKER:
        entry_fee = config.maker_fee_rate
        entry_slippage = 0.0
        entry_spread = 0.0
    else:
        entry_fee = config.taker_fee_rate
        entry_slippage = slippage_rate
        entry_spread = spread_rate / 2.0

    exit_fee = config.taker_fee_rate
    exit_slippage = slippage_rate
    exit_spread = spread_rate / 2.0

    return {
        "entry_fee_rate": entry_fee,
        "exit_fee_rate": exit_fee,
        "fee_rate_total": entry_fee + exit_fee,
        "entry_slippage_rate": entry_slippage,
        "exit_slippage_rate": exit_slippage,
        "slippage_rate_total": entry_slippage + exit_slippage,
        "spread_rate_total": entry_spread + exit_spread,
        "round_trip_rate": (
            entry_fee
            + exit_fee
            + entry_slippage
            + exit_slippage
            + entry_spread
            + exit_spread
        ),
    }


def evaluate_scenario(
    scenario: CostScenario,
    price: float,
    risk_amount: float,
    config: TradingCostConfig,
    spread_bps: float = ASSUMED_SPREAD_BPS,
    trades_per_month: float | None = None,
) -> dict[str, Any]:
    """
    Полная экономика одного сценария.

    required_gross_expectancy_r — сколько R стратегия обязана зарабатывать
    ДО издержек, чтобы выйти в ноль. Это и есть та планка, о которую
    разбиваются все три текущие реализации.
    """
    rates = execution_rates(scenario.execution_profile, config, spread_bps)

    stop_distance = price * scenario.stop_fraction

    if stop_distance <= 0:
        raise ValueError("stop distance must be > 0")

    quantity = risk_amount / stop_distance
    notional = quantity * price

    entry_fee = notional * rates["entry_fee_rate"]
    exit_fee = notional * rates["exit_fee_rate"]
    slippage = notional * rates["slippage_rate_total"]
    spread = notional * rates["spread_rate_total"]

    total_cost = entry_fee + exit_fee + slippage + spread

    cost_r = total_cost / risk_amount

    # Break-even по gross: сколько R нужно зарабатывать до издержек.
    required_gross_expectancy_r = cost_r

    def required_win_rate(gross_rr: float) -> float | None:
        """
        Win rate для безубытка при заданном gross R:R.

        Победа даёт (gross_rr - cost_r), поражение стоит (1 + cost_r):
        издержки платятся в обе стороны независимо от исхода. Если
        выигрыш после издержек не положителен, безубыток недостижим ни
        при какой доле побед — возвращается None, а не 100%.
        """
        win_value = gross_rr - cost_r
        loss_value = 1.0 + cost_r

        if win_value <= 0:
            return None

        return loss_value / (win_value + loss_value) * 100.0

    return {
        "scenario": scenario.name,
        "timeframe": scenario.timeframe,
        "stop_label": scenario.stop_label,
        "execution_profile": scenario.execution_profile,
        "price": round(price, 2),
        "stop_percent": round(scenario.stop_percent, 4),
        "stop_distance": round(stop_distance, 2),
        "risk_amount": risk_amount,
        "quantity": round(quantity, 8),
        "position_notional": round(notional, 2),
        "min_notional_ok": notional >= MIN_NOTIONAL_USDT,
        "entry_fee": round(entry_fee, 4),
        "exit_fee": round(exit_fee, 4),
        "spread_cost": round(spread, 4),
        "slippage_cost": round(slippage, 4),
        "total_round_trip_cost": round(total_cost, 4),
        "round_trip_rate": round(rates["round_trip_rate"], 6),
        "cost_r": round(cost_r, 4),
        "required_gross_expectancy_r": round(required_gross_expectancy_r, 4),
        "required_win_rate_rr2": (
            round(required_win_rate(2.0), 2)
            if required_win_rate(2.0) is not None
            else None
        ),
        "required_win_rate_rr3": (
            round(required_win_rate(3.0), 2)
            if required_win_rate(3.0) is not None
            else None
        ),
        "trades_per_month": trades_per_month,
        "monthly_cost": (
            round(total_cost * trades_per_month, 2)
            if trades_per_month is not None
            else None
        ),
        "fee_source": "config: api/paper_trading/cost_model.py",
        "spread_source": f"ASSUMPTION: {spread_bps} bps (not configured)",
        "slippage_source": (
            f"config: PAPER_SLIPPAGE_BPS={config.slippage_bps}"
        ),
    }


# ------------------------------------------------------- агрегация свечей


def aggregate_candles(candles: Sequence[Any], factor: int) -> list[Any]:
    """
    Склеивает свечи в более крупный таймфрейм.

    5m -> 15m это factor=3, 5m -> 1H это factor=12. Агрегация строго по
    порядку и без перекрытий: скользящее окно дало бы коррелированные бары
    и завысило бы число независимых наблюдений.
    """
    from api.strategy_engine.strategies.contracts import Candle

    aggregated: list[Candle] = []

    for start in range(0, len(candles) - factor + 1, factor):
        block = candles[start : start + factor]

        aggregated.append(
            Candle(
                open_time_ms=block[0].open_time_ms,
                open=block[0].open,
                high=max(item.high for item in block),
                low=min(item.low for item in block),
                close=block[-1].close,
                volume=sum(item.volume for item in block),
            )
        )

    return aggregated


def atr_percent_distribution(
    candles: Sequence[Any],
    period: int = 14,
    step: int = 25,
) -> dict[str, float]:
    """
    Распределение ATR в процентах цены.

    Медиана, а не среднее: распределение ATR имеет тяжёлый правый хвост,
    и среднее завысило бы типичную ширину стопа, то есть ЗАНИЗИЛО бы
    издержки в R. Ошибка в оптимистичную сторону здесь недопустима.
    """
    from api.strategy_engine.strategies.contracts import atr

    values: list[float] = []

    for index in range(period + 1, len(candles), step):
        window = candles[max(0, index - 60) : index + 1]

        value = atr(window, period)

        if value is not None and value > 0:
            values.append(value / candles[index].close * 100.0)

    if not values:
        return {}

    values.sort()

    return {
        "samples": len(values),
        "median_atr_percent": round(statistics.median(values), 4),
        "p25_atr_percent": round(values[len(values) // 4], 4),
        "p75_atr_percent": round(values[3 * len(values) // 4], 4),
        "median_price_to_atr": round(100.0 / statistics.median(values), 1),
    }
