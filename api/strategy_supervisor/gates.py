"""
Пороги и гейты супервизора. ЕДИНСТВЕННОЕ место, где живут числа.

ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ. Порог, размазанный по коду решений, невозможно
проверить: чтобы узнать, при каком profit factor стратегия
останавливается, пришлось бы читать всю логику. Здесь каждый порог —
именованная константа с обоснованием, а функции ниже только СРАВНИВАЮТ.

ПРИНЦИП НЕОСЛАБЛЕНИЯ. Ни одна функция здесь не смягчает критерий
автоматически. Если кандидат не прошёл — он не прошёл; «попробовать с
порогом пониже» невозможно, потому что порог не является аргументом
рантайма. Ослабить его можно только правкой кода с ревью.
"""

from __future__ import annotations

from typing import Any


# ------------------------------------------------- размер выборки (req 2)

# 30 закрытых сделок — минимум, при котором вообще имеет смысл говорить о
# win rate. Ниже доверительный интервал шире самого значения.
MIN_CLOSED_TRADES_WARNING = 30

# 50 — минимум для АВТОМАТИЧЕСКОЙ остановки. Порог выше, чем для warning,
# намеренно: остановка дороже предупреждения, поэтому требует больше
# доказательств.
MIN_CLOSED_TRADES_PAUSE = 50

# Несколько торговых дней: 50 сделок одного дня описывают один день, а не
# стратегию. Один аномальный день иначе решал бы судьбу стратегии.
MIN_TRADING_DAYS = 3

# Два режима — если стратегия заявлена больше чем для одного. Для
# стратегии с единственным разрешённым режимом требование неприменимо.
MIN_REGIMES_IF_DECLARED = 2


# ------------------------------------------------ остановка активной (req 3)

# Profit factor ниже 0.90 — устойчивый минус, а не шум. 1.0 в качестве
# порога останавливал бы стратегию при малейшем отклонении от нуля.
PAUSE_PROFIT_FACTOR_BELOW = 0.90

# Просадка в R, а не в деньгах: лимит обязан означать одно и то же
# независимо от капитала. 10R при риске 0.1% на сделку — это ~1% счёта.
PAUSE_MAX_DRAWDOWN_R = 10.0

# Шесть убытков подряд. При win rate 40% вероятность такой серии заметна,
# но в сочетании с прочими признаками это уже сигнал.
PAUSE_CONSECUTIVE_LOSSES = 6


# --------------------------------------------------- допуск кандидата (req 4)

PROMOTE_MIN_OOS_TRADES = 30
PROMOTE_MIN_PROFIT_FACTOR = 1.15
PROMOTE_MIN_NET_PNL = 0.0
PROMOTE_MIN_EXPECTANCY_R = 0.0
PROMOTE_MAX_DRAWDOWN_R = PAUSE_MAX_DRAWDOWN_R

# Устойчивость параметров. Кандидат обязан оставаться прибыльным не
# только в единственной точке, но и в её окрестности: если результат
# держится лишь при одном наборе значений, это подгонка, а не эффект.
PROMOTE_MIN_ROBUSTNESS_RATIO = 0.60


# ----------------------------------------------------------- cooldown (req 5)

SWITCH_COOLDOWN_DAYS = 7

# Возврат к отклонённой стратегии требует НОВОЙ независимой выборки, а не
# повторного прогона на тех же данных.
REJECTED_REQUIRES_NEW_SAMPLE_TRADES = PROMOTE_MIN_OOS_TRADES


INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"

SAFE = "SAFE"
WARNING = "WARNING"
PAUSED = "PAUSED"

NO_VALID_STRATEGY = "NO_VALID_STRATEGY"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def sample_adequacy(
    stats: dict[str, Any],
    declared_regime_count: int,
    for_pause: bool = False,
) -> dict[str, Any]:
    """
    Достаточна ли выборка для вывода.

    Возвращает и вердикт, и КАЖДОЕ несоблюдённое требование по отдельности
    — «мало сделок» и «все сделки одного дня» требуют разных действий, и
    склеивать их в один флаг значит терять эту разницу.
    """
    required_trades = (
        MIN_CLOSED_TRADES_PAUSE if for_pause else MIN_CLOSED_TRADES_WARNING
    )

    closed = stats.get("closed_trades") or 0
    days = stats.get("trading_days") or 0
    regimes = stats.get("regime_count") or 0

    # Требование по режимам применимо, только если стратегия заявлена
    # больше чем для одного режима.
    required_regimes = (
        MIN_REGIMES_IF_DECLARED if declared_regime_count > 1 else 1
    )

    missing: list[str] = []

    if closed < required_trades:
        missing.append(
            f"closed_trades={closed} < required {required_trades}"
        )

    if days < MIN_TRADING_DAYS:
        missing.append(
            f"trading_days={days} < required {MIN_TRADING_DAYS}"
        )

    if regimes < required_regimes:
        missing.append(
            f"regimes_observed={regimes} < required {required_regimes}"
        )

    return {
        "sufficient": not missing,
        "verdict": None if not missing else INSUFFICIENT_SAMPLE,
        "closed_trades": closed,
        "required_closed_trades": required_trades,
        "missing_closed_trades": max(0, required_trades - closed),
        "trading_days": days,
        "required_trading_days": MIN_TRADING_DAYS,
        "regimes_observed": regimes,
        "required_regimes": required_regimes,
        "unmet_requirements": missing,
    }


def pause_triggers(
    stats: dict[str, Any],
    *,
    out_of_regime: bool = False,
    repeated_failures: bool = False,
    contract_violations: bool = False,
) -> list[str]:
    """
    Причины остановки, выполняющиеся ПРИ ДОСТАТОЧНОЙ выборке.

    Функция намеренно не проверяет размер выборки: адекватность считается
    отдельно (sample_adequacy) и вызывающий обязан проверить её первой.
    Смешение этих двух вопросов и приводит к остановке стратегии по трём
    неудачным сделкам.
    """
    triggers: list[str] = []

    net_pnl = stats.get("net_pnl")

    if _is_number(net_pnl) and net_pnl < 0:
        triggers.append(f"net PnL after costs is negative ({net_pnl})")

    profit_factor = stats.get("profit_factor")

    if _is_number(profit_factor) and profit_factor < PAUSE_PROFIT_FACTOR_BELOW:
        triggers.append(
            f"profit factor {profit_factor} < {PAUSE_PROFIT_FACTOR_BELOW}"
        )

    expectancy = stats.get("expectancy_r")

    if _is_number(expectancy) and expectancy < 0:
        triggers.append(f"expectancy {expectancy}R is negative")

    drawdown = stats.get("max_drawdown_r")

    if _is_number(drawdown) and drawdown > PAUSE_MAX_DRAWDOWN_R:
        triggers.append(
            f"max drawdown {drawdown}R exceeds limit {PAUSE_MAX_DRAWDOWN_R}R"
        )

    streak = stats.get("max_loss_streak") or 0

    if streak >= PAUSE_CONSECUTIVE_LOSSES:
        triggers.append(
            f"{streak} consecutive losing trades "
            f"(limit {PAUSE_CONSECUTIVE_LOSSES})"
        )

    rolling = stats.get("rolling_expectancy_r")

    if _is_number(rolling) and rolling < 0:
        triggers.append(
            f"rolling {stats.get('rolling_window')}-trade expectancy "
            f"{rolling}R is negative"
        )

    if out_of_regime:
        triggers.append(
            "strategy traded outside its allowed market regime"
        )

    if repeated_failures:
        triggers.append("repeated FAILED_SAFELY records")

    if contract_violations:
        triggers.append("contract violations detected")

    return triggers


def promotion_gates(validation: dict[str, Any]) -> dict[str, Any]:
    """
    Проверяет кандидата по OOS-результатам.

    Все гейты обязательны. Кандидат не может «компенсировать» проваленный
    гейт выдающимся значением другого — именно так и выбирают стратегию по
    одной удачной выборке.
    """
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(
            {"gate": name, "passed": bool(passed), "detail": detail}
        )

    oos_trades = validation.get("oos_trades") or 0
    check(
        "min_oos_trades",
        oos_trades >= PROMOTE_MIN_OOS_TRADES,
        f"{oos_trades} OOS trades (required {PROMOTE_MIN_OOS_TRADES})",
    )

    net_pnl = validation.get("oos_net_pnl")
    check(
        "oos_net_pnl_positive",
        _is_number(net_pnl) and net_pnl > PROMOTE_MIN_NET_PNL,
        f"OOS net PnL {net_pnl}",
    )

    profit_factor = validation.get("oos_profit_factor")
    check(
        "min_profit_factor",
        _is_number(profit_factor)
        and profit_factor >= PROMOTE_MIN_PROFIT_FACTOR,
        f"OOS profit factor {profit_factor} "
        f"(required >= {PROMOTE_MIN_PROFIT_FACTOR})",
    )

    expectancy = validation.get("oos_expectancy_r")
    check(
        "positive_expectancy",
        _is_number(expectancy) and expectancy > PROMOTE_MIN_EXPECTANCY_R,
        f"OOS expectancy {expectancy}R",
    )

    drawdown = validation.get("oos_max_drawdown_r")
    check(
        "max_drawdown_within_limit",
        _is_number(drawdown) and drawdown <= PROMOTE_MAX_DRAWDOWN_R,
        f"OOS max drawdown {drawdown}R "
        f"(limit {PROMOTE_MAX_DRAWDOWN_R}R)",
    )

    violations = validation.get("safety_violations") or []
    check(
        "no_safety_violations",
        not violations,
        f"{len(violations)} safety violation(s)",
    )

    robustness = validation.get("robustness_ratio")
    check(
        "parameter_robustness",
        _is_number(robustness) and robustness >= PROMOTE_MIN_ROBUSTNESS_RATIO,
        f"robustness ratio {robustness} "
        f"(required >= {PROMOTE_MIN_ROBUSTNESS_RATIO})",
    )

    walk_forward = validation.get("walk_forward_passed")
    check(
        "walk_forward_passed",
        walk_forward is True,
        f"walk-forward passed: {walk_forward}",
    )

    leakage = validation.get("look_ahead_leakage")
    check(
        "no_look_ahead_leakage",
        leakage is False,
        f"look-ahead leakage: {leakage}",
    )

    failed = [item["gate"] for item in checks if not item["passed"]]

    return {
        "passed": not failed,
        "failed_gates": failed,
        "checks": checks,
    }


def thresholds_snapshot() -> dict[str, Any]:
    """Все пороги одним объектом — для эндпоинта и отчёта."""
    return {
        "sample": {
            "min_closed_trades_warning": MIN_CLOSED_TRADES_WARNING,
            "min_closed_trades_pause": MIN_CLOSED_TRADES_PAUSE,
            "min_trading_days": MIN_TRADING_DAYS,
            "min_regimes_if_declared": MIN_REGIMES_IF_DECLARED,
        },
        "pause": {
            "net_pnl_below": 0.0,
            "profit_factor_below": PAUSE_PROFIT_FACTOR_BELOW,
            "expectancy_below": 0.0,
            "max_drawdown_r": PAUSE_MAX_DRAWDOWN_R,
            "consecutive_losses": PAUSE_CONSECUTIVE_LOSSES,
            "rolling_expectancy_below": 0.0,
        },
        "promote": {
            "min_oos_trades": PROMOTE_MIN_OOS_TRADES,
            "min_profit_factor": PROMOTE_MIN_PROFIT_FACTOR,
            "min_net_pnl": PROMOTE_MIN_NET_PNL,
            "min_expectancy_r": PROMOTE_MIN_EXPECTANCY_R,
            "max_drawdown_r": PROMOTE_MAX_DRAWDOWN_R,
            "min_robustness_ratio": PROMOTE_MIN_ROBUSTNESS_RATIO,
        },
        "cooldown": {
            "switch_cooldown_days": SWITCH_COOLDOWN_DAYS,
            "rejected_requires_new_sample_trades": (
                REJECTED_REQUIRES_NEW_SAMPLE_TRADES
            ),
        },
    }
