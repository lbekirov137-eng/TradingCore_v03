"""
Adaptive Strategy Supervisor (PAPER MODE ONLY).

Пакет решает одну задачу: если активная стратегия статистически
подтверждённо убыточна — остановить её и выбрать замену из заранее
утверждённого реестра.

Границы, заданные структурно, а не соглашением:
  - выбор ограничен registry.STRATEGY_REGISTRY (литералы в коде);
  - параметры стратегий заморожены (frozen dataclass);
  - пороги — константы модуля gates и не являются аргументами рантайма;
  - функции супервизора чистые: не пишут на диск, не ходят в сеть, не
    трогают позиции; они возвращают РЕШЕНИЕ, исполняет его вызывающий;
  - автоматическое переключение возможно только в PAPER MODE.

Ни одна функция здесь не генерирует и не оптимизирует стратегию.
"""

from api.strategy_supervisor.gates import (
    INSUFFICIENT_SAMPLE,
    NO_VALID_STRATEGY,
    PAUSED,
    SAFE,
    WARNING,
    promotion_gates,
    sample_adequacy,
    pause_triggers,
    thresholds_snapshot,
)
from api.strategy_supervisor.registry import (
    CANDIDATE,
    DEFAULT_STRATEGY_ID,
    PAPER_ACTIVE,
    REJECTED,
    STRATEGY_REGISTRY,
    StrategyRegistryError,
    StrategySpec,
    all_strategies,
    get_strategy,
    is_registered,
    registry_snapshot,
    tradable_candidates,
)
from api.strategy_supervisor.stats import (
    ClosedTrade,
    build_stats,
    build_stats_from_observations,
    closed_trades_from_observations,
)
from api.strategy_supervisor.supervisor import (
    EMERGENCY_STOP,
    build_change_report,
    compare_champion_challengers,
    emergency_violations,
    evaluate_active_strategy,
    plan_switch,
    render_supervisor_section,
    select_replacement,
    supervisor_status,
)
from api.strategy_supervisor.validation import (
    ValidationError,
    build_walk_forward_windows,
    detect_look_ahead_leakage,
    robustness_ratio,
    split_holdout,
    validate_candidate,
)

__all__ = [
    "CANDIDATE",
    "PAPER_ACTIVE",
    "PAUSED",
    "REJECTED",
    "SAFE",
    "WARNING",
    "EMERGENCY_STOP",
    "INSUFFICIENT_SAMPLE",
    "NO_VALID_STRATEGY",
    "DEFAULT_STRATEGY_ID",
    "STRATEGY_REGISTRY",
    "StrategySpec",
    "StrategyRegistryError",
    "ClosedTrade",
    "ValidationError",
    "all_strategies",
    "get_strategy",
    "is_registered",
    "registry_snapshot",
    "tradable_candidates",
    "build_stats",
    "build_stats_from_observations",
    "closed_trades_from_observations",
    "sample_adequacy",
    "pause_triggers",
    "promotion_gates",
    "thresholds_snapshot",
    "evaluate_active_strategy",
    "emergency_violations",
    "select_replacement",
    "plan_switch",
    "compare_champion_challengers",
    "build_change_report",
    "render_supervisor_section",
    "supervisor_status",
    "split_holdout",
    "build_walk_forward_windows",
    "detect_look_ahead_leakage",
    "robustness_ratio",
    "validate_candidate",
]
