"""
CostViabilityGate — отказ от сделки, которую съедают издержки.

ЗАЧЕМ. Аудит показал: издержки в R равны cost_rate / stop_percent и не
зависят ни от риска, ни от размера счёта. На 5m с 1-ATR стопом это ~2.1R,
то есть стратегия платит вдвое больше, чем рискует, ещё до того, как рынок
куда-то пойдёт. Ни одна логика входа этого не компенсирует.

Гейт ставится ПЕРЕД созданием TradePlan и отвечает на единственный вопрос:
останется ли от сделки что-нибудь после трения. Он не улучшает сделку и
не подбирает параметры — он только отказывает.

ВАЖНО ПРО ПОРОГ. max_cost_r = 0.25 — консервативная отправная точка,
помеченная DEFAULT_NOT_OPTIMIZED. Она НЕ выведена из результатов прогона и
не должна подгоняться под то, чтобы «сделки проходили»: порог, ослабленный
ради прохождения, перестаёт быть порогом.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.paper_trading.cost_audit import (
    ASSUMED_SPREAD_BPS,
    MIN_NOTIONAL_USDT,
    TAKER_TAKER,
    execution_rates,
)
from api.paper_trading.cost_model import TradingCostConfig


# --------------------------------------------------------------- коды

COST_TOO_HIGH = "COST_TOO_HIGH"
STOP_TOO_TIGHT_FOR_COSTS = "STOP_TOO_TIGHT_FOR_COSTS"
NET_RR_TOO_LOW = "NET_RR_TOO_LOW"
SLIPPAGE_RISK_TOO_HIGH = "SLIPPAGE_RISK_TOO_HIGH"
MIN_NOTIONAL_CONFLICT = "MIN_NOTIONAL_CONFLICT"

COST_VIABLE = "COST_VIABLE"


@dataclass(frozen=True)
class CostGateConfig:
    """
    Пороги гейта. Неизменяемы: подбор порога под результат — это подгонка.

    Все значения помечены в docs как DEFAULT_NOT_OPTIMIZED, кроме
    min_net_rr, который следует проектному минимуму R:R.
    """

    # DEFAULT_NOT_OPTIMIZED — консервативная отправная точка.
    max_cost_r: float = 0.25

    # Минимальный ЧИСТЫЙ R:R после издержек. Проектный минимум gross —
    # 2.0; после издержек требуем не ниже 1.5, иначе сделка перестаёт
    # окупать риск.
    min_net_rr: float = 1.5

    # Стоп должен быть шире спреда с большим запасом: стоп шириной в
    # несколько спредов выбивается микроструктурным шумом, а не рынком.
    min_stop_to_spread_ratio: float = 20.0

    # Доля стопа, которую разрешено съесть проскальзыванию. Выше —
    # исполнение доминирует над решением.
    max_slippage_to_stop_ratio: float = 0.10

    min_notional_usdt: float = MIN_NOTIONAL_USDT
    assumed_spread_bps: float = ASSUMED_SPREAD_BPS

    execution_profile: str = TAKER_TAKER


def evaluate_cost_viability(
    *,
    entry: float,
    stop: float,
    take_profit: float,
    risk_amount: float,
    config: CostGateConfig | None = None,
    cost_config: TradingCostConfig | None = None,
) -> dict[str, Any]:
    """
    Считает экономику сделки и решает, допустима ли она.

    Возвращает ПОЛНЫЙ расчёт независимо от вердикта: отказ без чисел
    невозможно проверить, а именно эти числа объясняют, почему стратегия
    не торгует.
    """
    config = config or CostGateConfig()
    cost_config = cost_config or TradingCostConfig()

    if entry <= 0 or stop <= 0 or take_profit <= 0:
        return _refuse(
            COST_TOO_HIGH,
            "non-positive price levels",
            entry=entry,
            stop=stop,
            take_profit=take_profit,
        )

    stop_distance = entry - stop
    reward_distance = take_profit - entry

    if stop_distance <= 0 or reward_distance <= 0:
        return _refuse(
            NET_RR_TOO_LOW,
            "levels do not form a long trade",
            stop_distance=stop_distance,
            reward_distance=reward_distance,
        )

    rates = execution_rates(
        config.execution_profile, cost_config, config.assumed_spread_bps
    )

    quantity = risk_amount / stop_distance
    notional = quantity * entry

    total_cost = notional * rates["round_trip_rate"]
    cost_r = total_cost / risk_amount if risk_amount > 0 else float("inf")

    gross_rr = reward_distance / stop_distance
    # Издержки платятся при любом исходе, поэтому вычитаются из награды
    # И добавляются к риску — иначе чистый R:R был бы завышен.
    net_reward_r = gross_rr - cost_r
    net_rr_after_costs = net_reward_r / (1.0 + cost_r)

    stop_percent = stop_distance / entry * 100.0
    spread_fraction = config.assumed_spread_bps / 10_000.0
    spread_distance = entry * spread_fraction
    slippage_distance = entry * cost_config.slippage_rate

    detail = {
        "estimated_round_trip_cost": round(total_cost, 6),
        "estimated_cost_r": round(cost_r, 4),
        "net_reward_r": round(net_reward_r, 4),
        "net_rr_after_costs": round(net_rr_after_costs, 4),
        "gross_rr": round(gross_rr, 4),
        "position_notional": round(notional, 2),
        "quantity": round(quantity, 8),
        "stop_percent": round(stop_percent, 4),
        "round_trip_rate": round(rates["round_trip_rate"], 6),
        "execution_profile": config.execution_profile,
        "max_cost_r": config.max_cost_r,
        "min_net_rr": config.min_net_rr,
    }

    # Порядок проверок — от самой структурной причины к производной, чтобы
    # reason_code называл ПЕРВОПРИЧИНУ, а не следствие.

    if notional < config.min_notional_usdt:
        return _refuse(
            MIN_NOTIONAL_CONFLICT,
            f"notional {notional:.2f} below exchange minimum "
            f"{config.min_notional_usdt}",
            **detail,
        )

    if stop_distance < spread_distance * config.min_stop_to_spread_ratio:
        return _refuse(
            STOP_TOO_TIGHT_FOR_COSTS,
            f"stop {stop_distance:.2f} is under "
            f"{config.min_stop_to_spread_ratio}x the assumed spread "
            f"{spread_distance:.4f}",
            **detail,
        )

    if slippage_distance > stop_distance * config.max_slippage_to_stop_ratio:
        return _refuse(
            SLIPPAGE_RISK_TOO_HIGH,
            f"expected slippage {slippage_distance:.2f} exceeds "
            f"{config.max_slippage_to_stop_ratio:.0%} of the stop distance",
            **detail,
        )

    if cost_r > config.max_cost_r:
        return _refuse(
            COST_TOO_HIGH,
            f"round-trip cost {cost_r:.4f}R exceeds the limit "
            f"{config.max_cost_r}R",
            **detail,
        )

    if net_rr_after_costs < config.min_net_rr:
        return _refuse(
            NET_RR_TOO_LOW,
            f"net R:R {net_rr_after_costs:.4f} after costs is below "
            f"{config.min_net_rr}",
            **detail,
        )

    return {"viable": True, "reason_code": COST_VIABLE, "reason": None, **detail}


def _refuse(reason_code: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"viable": False, "reason_code": reason_code, "reason": reason, **detail}


def required_stop_percent(
    max_cost_r: float = 0.25,
    execution_profile: str = TAKER_TAKER,
    cost_config: TradingCostConfig | None = None,
    spread_bps: float = ASSUMED_SPREAD_BPS,
) -> float:
    """
    Минимальная ширина стопа (в % цены) для заданного предела издержек.

    Прямое следствие тождества cost_r = cost_rate / stop_percent. Функция
    существует, чтобы требование «cost_r <= 0.25R» можно было перевести в
    проверяемое «стоп не уже X%», а не подбирать вслепую.
    """
    cost_config = cost_config or TradingCostConfig()

    rate = execution_rates(execution_profile, cost_config, spread_bps)[
        "round_trip_rate"
    ]

    return rate / max_cost_r * 100.0
