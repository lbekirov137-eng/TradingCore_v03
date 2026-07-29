"""
Adaptive Strategy Supervisor для PAPER MODE.

ЧТО ОН ДЕЛАЕТ. Наблюдает за активной стратегией, и если она статистически
подтверждённо убыточна — останавливает её и выбирает замену из реестра.

ЧЕГО ОН НЕ ДЕЛАЕТ И НЕ МОЖЕТ ДЕЛАТЬ ПО УСТРОЙСТВУ:
  - не придумывает стратегии: выбор ограничен api/strategy_supervisor/registry;
  - не меняет параметры: StrategySpec заморожен, setter'ов нет;
  - не оптимизирует ничего в рабочем цикле: здесь нет ни одной функции,
    которая подбирает значения по результатам;
  - не ослабляет пороги: они константы модуля gates, а не аргументы;
  - не переключается при открытой позиции;
  - не работает нигде, кроме PAPER: live-режим — это авария (req 9).

РЕШЕНИЕ ЧИСТОЕ. Ни одна функция здесь не пишет на диск, не ходит в сеть и
не трогает позиции. Она возвращает РЕШЕНИЕ, а исполняет его вызывающий.
Это делает всю логику тестируемой без запуска цикла и гарантирует, что
супервизор не может стать источником побочного эффекта в торговом пути.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from api.strategy_supervisor import gates
from api.strategy_supervisor.registry import (
    DEFAULT_STRATEGY_ID,
    PAPER_ACTIVE,
    StrategySpec,
    all_strategies,
    get_strategy,
    is_registered,
)
from api.strategy_supervisor.stats import build_stats_from_observations


SAFE = gates.SAFE
WARNING = gates.WARNING
PAUSED = gates.PAUSED
NO_VALID_STRATEGY = gates.NO_VALID_STRATEGY
INSUFFICIENT_SAMPLE = gates.INSUFFICIENT_SAMPLE

EMERGENCY_STOP = "EMERGENCY_STOP"

# Сколько FAILED_SAFELY подряд считается повторением (req 3, req 9).
FAILED_SAFELY_STREAK = 2

_SHORT_MARKERS = {"SHORT", "SELL"}
_TRADABLE_SIGNALS = {"BUY", "SELL"}


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------
# Аварийные правила (req 9)
# ---------------------------------------------------------------------


def emergency_violations(
    observations: Sequence[dict[str, Any]],
    *,
    strategy_changed_with_open_position: bool = False,
    promoted_without_passing_gates: bool = False,
) -> list[str]:
    """
    Нарушения, требующие НЕМЕДЛЕННОЙ остановки всего монитора.

    Это не метрики и не «плохой результат» — это признаки того, что
    система делает не то, что заявлено. Поэтому они проверяются БЕЗ
    требований к размеру выборки: одного случая достаточно. Ждать
    статистической значимости отправленного реального ордера бессмысленно.
    """
    violations: list[str] = []

    real_orders = [
        item for item in observations
        if item.get("real_order_sent") is True
    ]

    if real_orders:
        violations.append(
            f"real_order_sent=True in {len(real_orders)} record(s)"
        )

    shorts = [
        item for item in observations
        if (item.get("side") or "").upper() in _SHORT_MARKERS
        or (item.get("signal") or "").upper() == "SELL"
    ]

    if shorts:
        violations.append(
            f"SHORT/SELL direction observed in {len(shorts)} record(s)"
        )

    contradictions = 0

    for item in observations:
        decision = item.get("decision")
        signal = item.get("signal")

        if decision == "TRADE" and signal not in _TRADABLE_SIGNALS:
            contradictions += 1
        elif (
            decision == "NO_TRADE"
            and item.get("position_event") == "POSITION_OPENED"
        ):
            contradictions += 1

    if contradictions:
        violations.append(
            f"signal/decision contradiction in {contradictions} record(s)"
        )

    streak = 0
    longest = 0

    for item in observations:
        if item.get("failed_safely"):
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    if longest >= FAILED_SAFELY_STREAK:
        violations.append(
            f"FAILED_SAFELY repeated {longest} times in a row"
        )

    if strategy_changed_with_open_position:
        violations.append(
            "strategy was changed while a position was open"
        )

    if promoted_without_passing_gates:
        violations.append(
            "a strategy was promoted without passing OOS gates"
        )

    return violations


# ---------------------------------------------------------------------
# Оценка активной стратегии (req 2, 3)
# ---------------------------------------------------------------------


def evaluate_active_strategy(
    strategy_id: str,
    observations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """
    Решает, что делать с текущей стратегией: SAFE / WARNING / PAUSED.

    Порядок проверок принципиален:
      1. аварии — они не зависят от объёма выборки;
      2. достаточность выборки — без неё вывод о прибыльности запрещён;
      3. только потом пороги остановки.

    Перестановка шагов 2 и 3 и есть «признать стратегию убыточной слишком
    рано»: пять неудачных сделок дают и отрицательный net, и profit factor
    ниже порога, и по ним нельзя останавливать ничего.
    """
    spec = get_strategy(strategy_id)

    stats = build_stats_from_observations(observations)

    violations = emergency_violations(observations)

    out_of_regime = [
        item.get("market_regime")
        for item in observations
        if item.get("position_event") == "POSITION_OPENED"
        and item.get("market_regime") is not None
        and not spec.allows_regime(item.get("market_regime"))
    ]

    failed_safely_total = sum(
        1 for item in observations if item.get("failed_safely")
    )

    # Достаточность считается для ОСТАНОВКИ (порог 50), потому что именно
    # остановка — то решение, которое здесь может быть принято.
    adequacy = gates.sample_adequacy(
        stats,
        declared_regime_count=len(spec.allowed_regimes),
        for_pause=True,
    )

    warning_adequacy = gates.sample_adequacy(
        stats,
        declared_regime_count=len(spec.allowed_regimes),
        for_pause=False,
    )

    triggers = gates.pause_triggers(
        stats,
        out_of_regime=bool(out_of_regime),
        repeated_failures=any(
            "FAILED_SAFELY repeated" in violation
            for violation in violations
        ),
        contract_violations=any(
            "contradiction" in violation or "real_order_sent" in violation
            for violation in violations
        ),
    )

    if violations:
        status = EMERGENCY_STOP
        reasons = violations
    elif adequacy["sufficient"] and triggers:
        status = PAUSED
        reasons = triggers
    elif warning_adequacy["sufficient"] and triggers:
        # Выборки хватает на предупреждение, но не на остановку.
        status = WARNING
        reasons = triggers
    elif triggers:
        # Признаки есть, но выборка мала — это НЕ повод останавливать.
        status = SAFE
        reasons = []
    else:
        status = SAFE
        reasons = []

    return {
        "strategy_id": spec.strategy_id,
        "strategy_key": spec.key,
        "status": status,
        "reasons": reasons,
        "observed_triggers": triggers,
        "sample": adequacy,
        "sample_for_warning": warning_adequacy,
        "stats": stats,
        "emergency_violations": violations,
        "out_of_regime_trades": len(out_of_regime),
        "failed_safely_count": failed_safely_total,
        "allowed_regimes": list(spec.allowed_regimes),
        "insufficient_sample": (
            INSUFFICIENT_SAMPLE if not adequacy["sufficient"] else None
        ),
    }


# ---------------------------------------------------------------------
# Выбор замены (req 4, 5)
# ---------------------------------------------------------------------


def _cooldown_blocked(
    last_switch_at_utc: Any,
    now_utc: Any,
) -> tuple[bool, str | None]:
    last = _parse_utc(last_switch_at_utc)
    now = _parse_utc(now_utc)

    if last is None or now is None:
        return False, None

    earliest = last + timedelta(days=gates.SWITCH_COOLDOWN_DAYS)

    if now < earliest:
        return True, (
            f"cooldown active: last switch {last.isoformat()}, "
            f"next allowed {earliest.isoformat()}"
        )

    return False, None


def select_replacement(
    validations: Mapping[str, dict[str, Any]],
    *,
    exclude: Iterable[str] = (),
    rejected: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Выбирает замену среди ЗАРЕГИСТРИРОВАННЫХ кандидатов.

    Выбор двухступенчатый и это важно:
      1. отсеиваются все, кто не прошёл хотя бы один гейт;
      2. и только среди прошедших выбирается лучший.

    Ранжирование идёт по expectancy, а НЕ по общей доходности: максимальная
    прибыль одной выборки — то самое переобучение, которое запрещено
    (req 7). При равенстве expectancy предпочитается меньшая просадка.

    Незарегистрированный идентификатор игнорируется, а не выбирается: это
    последний барьер против попытки подсунуть стратегию мимо реестра.
    """
    rejected = rejected or {}
    excluded = set(exclude)

    evaluated: list[dict[str, Any]] = []

    for strategy_id, validation in validations.items():
        if not is_registered(strategy_id):
            evaluated.append(
                {
                    "strategy_id": strategy_id,
                    "eligible": False,
                    "reason": "not registered in the strategy registry",
                    "gates": None,
                }
            )
            continue

        if strategy_id in excluded:
            evaluated.append(
                {
                    "strategy_id": strategy_id,
                    "eligible": False,
                    "reason": "excluded (currently active or paused)",
                    "gates": None,
                }
            )
            continue

        result = gates.promotion_gates(validation)

        # Ранее отклонённая стратегия требует НОВОЙ независимой выборки.
        previous = rejected.get(strategy_id)

        if previous is not None:
            sample_id = validation.get("sample_id")
            previous_sample = previous.get("sample_id")

            if sample_id is None or sample_id == previous_sample:
                evaluated.append(
                    {
                        "strategy_id": strategy_id,
                        "eligible": False,
                        "reason": (
                            "previously rejected; requires a NEW independent "
                            "sample (same or missing sample_id)"
                        ),
                        "gates": result,
                    }
                )
                continue

        evaluated.append(
            {
                "strategy_id": strategy_id,
                "eligible": result["passed"],
                "reason": (
                    "passed all gates"
                    if result["passed"]
                    else "failed gates: " + ", ".join(result["failed_gates"])
                ),
                "gates": result,
                "expectancy_r": validation.get("oos_expectancy_r"),
                "profit_factor": validation.get("oos_profit_factor"),
                "max_drawdown_r": validation.get("oos_max_drawdown_r"),
                "oos_trades": validation.get("oos_trades"),
            }
        )

    eligible = [item for item in evaluated if item.get("eligible")]

    if not eligible:
        return {
            "selected": None,
            "status": NO_VALID_STRATEGY,
            "fallback_strategy_id": DEFAULT_STRATEGY_ID,
            "reason": (
                "no registered candidate passed every gate; staying in "
                "NO_TRADE. Thresholds are NOT relaxed automatically."
            ),
            "evaluated": evaluated,
        }

    eligible.sort(
        key=lambda item: (
            -(item.get("expectancy_r") or 0.0),
            item.get("max_drawdown_r") or 0.0,
        )
    )

    winner = eligible[0]

    return {
        "selected": winner["strategy_id"],
        "status": "CANDIDATE_SELECTED",
        "fallback_strategy_id": None,
        "reason": (
            f"highest OOS expectancy among {len(eligible)} qualified "
            "candidate(s); ranked by expectancy, not by total return"
        ),
        "evaluated": evaluated,
    }


def plan_switch(
    *,
    active_strategy_id: str,
    evaluation: dict[str, Any],
    validations: Mapping[str, dict[str, Any]],
    has_open_position: bool,
    now_utc: str,
    last_switch_at_utc: str | None = None,
    rejected: Mapping[str, dict[str, Any]] | None = None,
    paper_mode: bool = True,
) -> dict[str, Any]:
    """
    Строит ПЛАН переключения. Ничего не исполняет.

    Отказы упорядочены от самого жёсткого к самому мягкому, и первый же
    сработавший останавливает рассмотрение — так план не может «пройти»
    по совокупности слабых доводов.
    """
    def refuse(action: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "action": action,
            "switch_allowed": False,
            "from_strategy_id": active_strategy_id,
            "to_strategy_id": None,
            "reason": reason,
            **extra,
        }

    if not paper_mode:
        # Автоматическое переключение разрешено ТОЛЬКО в paper (req 5).
        return refuse(
            EMERGENCY_STOP,
            "automatic strategy switching is allowed in PAPER MODE only",
        )

    if evaluation.get("emergency_violations"):
        return refuse(
            EMERGENCY_STOP,
            "emergency violations must be resolved by a human before any "
            "switch",
            violations=evaluation["emergency_violations"],
        )

    if evaluation.get("status") not in (PAUSED,):
        return refuse(
            "HOLD",
            f"active strategy status is {evaluation.get('status')}; "
            "a switch is only considered after PAUSED",
            insufficient_sample=evaluation.get("insufficient_sample"),
        )

    if has_open_position:
        # Стратегия НИКОГДА не меняется внутри открытой позиции (req 3, 9).
        return refuse(
            "WAIT_FOR_FLAT",
            "a paper position is still open; the switch waits for it to "
            "close safely. Changing strategy mid-position is an emergency "
            "condition, not a transition.",
        )

    blocked, cooldown_reason = _cooldown_blocked(last_switch_at_utc, now_utc)

    if blocked:
        return refuse("COOLDOWN", cooldown_reason)

    selection = select_replacement(
        validations,
        exclude=(active_strategy_id,),
        rejected=rejected,
    )

    if selection["selected"] is None:
        return {
            "action": "NO_VALID_STRATEGY",
            "switch_allowed": False,
            "from_strategy_id": active_strategy_id,
            "to_strategy_id": None,
            "fallback_strategy_id": selection["fallback_strategy_id"],
            "reason": selection["reason"],
            "selection": selection,
        }

    return {
        "action": "SWITCH",
        "switch_allowed": True,
        "from_strategy_id": active_strategy_id,
        "to_strategy_id": selection["selected"],
        "reason": selection["reason"],
        "selection": selection,
        "pause_reasons": evaluation.get("reasons", []),
    }


# ---------------------------------------------------------------------
# Champion / Challenger (req 6)
# ---------------------------------------------------------------------


def compare_champion_challengers(
    champion_id: str,
    challenger_decisions: Mapping[str, Sequence[dict[str, Any]]],
    champion_decisions: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """
    Сравнивает решения на ОДНИХ И ТЕХ ЖЕ свечах.

    Challenger работает в shadow mode: он выдаёт решения, но не открывает
    даже paper-позицию. Сравнение по свечам, а не по итоговым метрикам,
    показывает, ГДЕ стратегии расходятся, — итоговая разница в net этого
    не объясняет.

    Свечи сопоставляются по recorded_at_utc. Решение challenger'а без
    парной свечи champion'а отбрасывается: сравнивать надо одинаковые
    условия, иначе преимущество может быть просто разным набором свечей.
    """
    champion_by_time = {
        item.get("recorded_at_utc"): item
        for item in champion_decisions
        if item.get("recorded_at_utc")
    }

    comparisons: dict[str, Any] = {}

    for challenger_id, decisions in challenger_decisions.items():
        matched = 0
        agree = 0
        challenger_trades_champion_skips = 0
        champion_trades_challenger_skips = 0

        for item in decisions:
            stamp = item.get("recorded_at_utc")
            champion_item = champion_by_time.get(stamp)

            if champion_item is None:
                continue

            matched += 1

            champion_decision = champion_item.get("decision")
            challenger_decision = item.get("decision")

            if champion_decision == challenger_decision:
                agree += 1
            elif challenger_decision == "TRADE":
                challenger_trades_champion_skips += 1
            elif champion_decision == "TRADE":
                champion_trades_challenger_skips += 1

        comparisons[challenger_id] = {
            "registered": is_registered(challenger_id),
            "shadow_mode": True,
            "paper_positions_opened": 0,
            "matched_candles": matched,
            "agreement_count": agree,
            "agreement_percent": (
                round(agree / matched * 100.0, 2) if matched else None
            ),
            "challenger_trades_champion_skips": (
                challenger_trades_champion_skips
            ),
            "champion_trades_challenger_skips": (
                champion_trades_challenger_skips
            ),
        }

    return {
        "champion_id": champion_id,
        "champion_candles": len(champion_by_time),
        "challengers": comparisons,
        "note": (
            "Challengers never open a position, not even a paper one. "
            "Promotion requires passing every gate in gates.promotion_gates."
        ),
    }


# ---------------------------------------------------------------------
# Strategy Change Report (req 5, 8)
# ---------------------------------------------------------------------


def build_change_report(
    *,
    plan: dict[str, Any],
    evaluation: dict[str, Any],
    now_utc: str,
    validations: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Отчёт о переключении (или об отказе от него).

    Пишется В ЛЮБОМ случае, включая отказ: «почему НЕ переключились» —
    такой же важный факт, как и само переключение, и без него молчание
    системы неотличимо от её поломки.
    """
    validations = validations or {}
    to_id = plan.get("to_strategy_id")

    return {
        "schema_version": "STRATEGY_CHANGE_REPORT_V1",
        "generated_at_utc": now_utc,
        "mode": "PAPER",
        "real_orders_enabled": False,
        "action": plan.get("action"),
        "switch_allowed": plan.get("switch_allowed", False),
        "from_strategy": {
            "strategy_id": evaluation.get("strategy_id"),
            "strategy_key": evaluation.get("strategy_key"),
            "status": evaluation.get("status"),
            "pause_reasons": evaluation.get("reasons", []),
            "stats": evaluation.get("stats"),
            "sample": evaluation.get("sample"),
        },
        "to_strategy": (
            {
                "strategy_id": to_id,
                "strategy_key": get_strategy(to_id).key,
                "validation": validations.get(to_id),
                "gates": next(
                    (
                        item.get("gates")
                        for item in plan.get("selection", {}).get(
                            "evaluated", []
                        )
                        if item.get("strategy_id") == to_id
                    ),
                    None,
                ),
            }
            if to_id and is_registered(to_id)
            else None
        ),
        "fallback_strategy_id": plan.get("fallback_strategy_id"),
        "reason": plan.get("reason"),
        "rejected_candidates": [
            {
                "strategy_id": item.get("strategy_id"),
                "reason": item.get("reason"),
                "failed_gates": (item.get("gates") or {}).get(
                    "failed_gates", []
                ),
            }
            for item in plan.get("selection", {}).get("evaluated", [])
            if not item.get("eligible")
        ],
        "thresholds": gates.thresholds_snapshot(),
        "cooldown_days": gates.SWITCH_COOLDOWN_DAYS,
        "note": (
            "Previous strategy is retained in the registry for analysis "
            "and is never deleted."
        ),
    }


def render_supervisor_section(
    status: dict[str, Any],
    *,
    challengers: Mapping[str, Any] | None = None,
    last_change_reason: str | None = None,
) -> str:
    """
    Блок супервизора для DAILY PAPER REPORT.

    Размер выборки печатается СРАЗУ после статуса: иначе expectancy и
    profit factor прочитают раньше, чем узнают, что они посчитаны по
    четырём сделкам.
    """
    evaluation = status.get("evaluation", {})
    stats = evaluation.get("stats", {})
    sample = evaluation.get("sample", {})
    champion = status.get("champion", {})

    def show(value: Any, suffix: str = "") -> str:
        return "n/a" if value is None else f"{value}{suffix}"

    line = "-" * 68

    out = [
        line,
        "STRATEGY SUPERVISOR",
        line,
        f"CHAMPION: {champion.get('strategy_key')}",
        f"STATUS:   {champion.get('status')}",
    ]

    for reason in champion.get("reasons", []) or []:
        out.append(f"  - {reason}")

    if not sample.get("sufficient"):
        out.append("")
        out.append(
            f"{INSUFFICIENT_SAMPLE}: "
            f"closed={sample.get('closed_trades')} "
            f"required={sample.get('required_closed_trades')} "
            f"days={sample.get('trading_days')}/"
            f"{sample.get('required_trading_days')} "
            f"regimes={sample.get('regimes_observed')}/"
            f"{sample.get('required_regimes')}"
        )

    out.append("")
    out.append(f"CLOSED TRADES:  {show(stats.get('closed_trades'))}")
    out.append(f"NET PnL:        {show(stats.get('net_pnl'))}")
    out.append(f"PROFIT FACTOR:  {show(stats.get('profit_factor'))}")
    out.append(f"EXPECTANCY:     {show(stats.get('expectancy_r'), 'R')}")
    out.append(f"MAX DRAWDOWN:   {show(stats.get('max_drawdown_r'), 'R')}")
    out.append(
        f"ROLLING {stats.get('rolling_window', 20)}:     "
        f"expectancy={show(stats.get('rolling_expectancy_r'), 'R')} "
        f"(trades={show(stats.get('rolling_trades'))})"
    )
    out.append(f"LOSS STREAK:    {show(stats.get('max_loss_streak'))}")

    out.append("")

    if challengers:
        out.append("CHALLENGERS (shadow mode, no positions):")

        for name, data in challengers.items():
            if isinstance(data, dict):
                out.append(
                    f"  {name}: agreement="
                    f"{show(data.get('agreement_percent'), '%')} "
                    f"matched={show(data.get('matched_candles'))} "
                    f"positions={data.get('paper_positions_opened', 0)}"
                )
            else:
                out.append(f"  {name}: {data}")
    else:
        out.append("CHALLENGERS: none registered for shadow comparison")

    out.append("")
    out.append(
        f"LAST CHANGE: {last_change_reason or 'no strategy change recorded'}"
    )
    out.append(line)

    return "\n".join(out)


def supervisor_status(
    *,
    champion_id: str,
    observations: Sequence[dict[str, Any]],
    has_open_position: bool = False,
) -> dict[str, Any]:
    """Сводный статус супервизора для эндпоинта и отчёта."""
    evaluation = evaluate_active_strategy(champion_id, observations)

    return {
        "schema_version": "STRATEGY_SUPERVISOR_V1",
        "mode": "PAPER",
        "real_orders_enabled": False,
        "automatic_switching": "PAPER_ONLY",
        "champion": {
            "strategy_id": champion_id,
            "strategy_key": evaluation["strategy_key"],
            "status": evaluation["status"],
            "reasons": evaluation["reasons"],
        },
        "has_open_position": has_open_position,
        "evaluation": evaluation,
        "thresholds": gates.thresholds_snapshot(),
        "registry": [
            {
                "strategy_id": spec.strategy_id,
                "version": spec.version,
                "status": (
                    PAPER_ACTIVE
                    if spec.strategy_id == champion_id
                    else spec.status
                ),
                "tradable": spec.tradable,
                "allowed_regimes": list(spec.allowed_regimes),
                # Ни одна стратегия реестра не одобрена для реальных
                # денег. Поле присутствует ЯВНО, чтобы это можно было
                # проверить снаружи, а не выводить из отсутствия флага.
                "production_approved": False,
            }
            for spec in all_strategies()
        ],
        "guarantees": [
            "The supervisor may only select from the code-defined registry.",
            "Strategy parameters are frozen and cannot be tuned at runtime.",
            "No strategy is generated, rewritten or optimised by a model.",
            "Thresholds are constants and are never relaxed automatically.",
            "No switch occurs while a position is open.",
        ],
    }
