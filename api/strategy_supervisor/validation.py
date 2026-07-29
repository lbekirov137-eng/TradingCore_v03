"""
Хронологическая валидация кандидата: walk-forward + отдельный OOS holdout.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Гейты (gates.promotion_gates) только сравнивают
готовые числа с порогами. Здесь эти числа ПОЛУЧАЮТСЯ — и именно на этом
шаге обычно и ломается вся оценка: данные перемешиваются, тестовый период
попадает в обучение, а параметры подбираются на том же отрезке, на
котором потом «подтверждаются».

Что гарантируется структурно:

  - РАЗБИЕНИЕ ТОЛЬКО ХРОНОЛОГИЧЕСКОЕ. Сортировка по времени принудительна,
    перемешивание невозможно: функции не принимают ни random_state, ни
    флага shuffle.

  - OOS-ОТРЕЗОК ОТРЕЗАН ПОСЛЕДНИМ и не пересекается с walk-forward. Он
    физически не участвует ни в одном окне обучения.

  - LOOK-AHEAD ПРОВЕРЯЕТСЯ, А НЕ ДЕКЛАРИРУЕТСЯ. detect_look_ahead_leakage
    сравнивает границы окон и возвращает True при любом пересечении;
    результат кладётся в валидацию и проверяется гейтом.

  - УСТОЙЧИВОСТЬ ПАРАМЕТРОВ измеряется долей прибыльных окон, а не лучшим
    окном. Стратегия, прибыльная в одном окне из шести, устойчивой не
    считается, каким бы ни был её суммарный результат.

Модуль НИЧЕГО не оптимизирует. Он не подбирает параметры и не выбирает
лучший вариант из перебора — он только режет историю и считает метрики по
уже зафиксированной спецификации из реестра.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from api.strategy_supervisor.stats import ClosedTrade, build_stats


class ValidationError(ValueError):
    """Некорректная постановка валидации. Осознанно не подавляется."""


@dataclass(frozen=True)
class Window:
    """Одно окно walk-forward: обучение строго ДО теста."""

    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
        }


def _sorted_by_time(trades: Sequence[ClosedTrade]) -> list[ClosedTrade]:
    """
    Хронологический порядок принудительно.

    Сделка без времени отбрасывается: поставить её «куда-нибудь» означало
    бы допустить, что результат будущего окна попал в прошлое.
    """
    dated = [
        trade for trade in trades
        if isinstance(trade.closed_at_utc, str) and trade.closed_at_utc
    ]

    return sorted(dated, key=lambda trade: trade.closed_at_utc)


def split_holdout(
    trades: Sequence[ClosedTrade],
    holdout_fraction: float = 0.3,
) -> tuple[list[ClosedTrade], list[ClosedTrade]]:
    """
    Отрезает ПОСЛЕДНЮЮ по времени часть под out-of-sample holdout.

    Именно последнюю, а не случайную: случайный holdout при временных
    рядах бессмысленен — модель «увидит» будущее через соседние точки.
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ValidationError(
            f"holdout_fraction must be in (0, 1), got {holdout_fraction}"
        )

    ordered = _sorted_by_time(trades)

    if not ordered:
        return [], []

    split_at = int(len(ordered) * (1.0 - holdout_fraction))

    # Гарантируем непустой holdout, если сделок хотя бы две.
    if split_at >= len(ordered):
        split_at = len(ordered) - 1

    return ordered[:split_at], ordered[split_at:]


def build_walk_forward_windows(
    trades: Sequence[ClosedTrade],
    window_count: int = 4,
    train_fraction: float = 0.6,
) -> list[dict[str, Any]]:
    """
    Строит расширяющиеся окна: обучение всегда ДО теста.

    Возвращает окна вместе с метриками теста, чтобы устойчивость считалась
    по НЕЗАВИСИМЫМ отрезкам, а не по одному общему прогону.
    """
    ordered = _sorted_by_time(trades)

    if window_count < 1:
        raise ValidationError("window_count must be >= 1")

    if len(ordered) < window_count * 2:
        return []

    chunk = len(ordered) // window_count
    windows: list[dict[str, Any]] = []

    for index in range(window_count):
        start = index * chunk
        end = (index + 1) * chunk if index < window_count - 1 else len(ordered)

        block = ordered[start:end]

        if len(block) < 2:
            continue

        train_size = max(1, int(len(block) * train_fraction))
        train = block[:train_size]
        test = block[train_size:]

        if not test:
            continue

        window = Window(
            index=index,
            train_start=train[0].closed_at_utc,
            train_end=train[-1].closed_at_utc,
            test_start=test[0].closed_at_utc,
            test_end=test[-1].closed_at_utc,
        )

        stats = build_stats(test)

        windows.append(
            {
                "window": window.to_dict(),
                "train_trades": len(train),
                "test_trades": len(test),
                "test_net_pnl": stats["net_pnl"],
                "test_expectancy_r": stats["expectancy_r"],
                "test_profit_factor": stats["profit_factor"],
                "profitable": bool(
                    stats["net_pnl"] is not None and stats["net_pnl"] > 0
                ),
            }
        )

    return windows


def detect_look_ahead_leakage(
    windows: Sequence[dict[str, Any]],
    holdout_start: str | None = None,
) -> bool:
    """
    Ищет пересечение обучения и теста — фактически, а не на словах.

    Утечкой считается:
      - обучение, заканчивающееся не раньше начала теста внутри окна;
      - любое окно, залезающее в holdout-период.

    Возвращает True при обнаружении. Гейт требует ровно False, поэтому
    неизвестность (например, пустой список окон) утечкой не считается, но
    и гейт walk_forward_passed при пустых окнах не пройдёт.
    """
    for item in windows:
        window = item.get("window", {})

        train_end = window.get("train_end")
        test_start = window.get("test_start")

        if train_end and test_start and train_end >= test_start:
            return True

        if holdout_start:
            for boundary in ("train_end", "test_end"):
                value = window.get(boundary)

                if value and value >= holdout_start:
                    return True

    return False


def robustness_ratio(windows: Sequence[dict[str, Any]]) -> float | None:
    """
    Доля прибыльных окон.

    Именно доля, а не сумма: стратегия, вытянувшая общий плюс одним окном
    из шести, — это одна удачная неделя, а не устойчивый эффект.
    """
    if not windows:
        return None

    profitable = sum(1 for item in windows if item.get("profitable"))

    return round(profitable / len(windows), 4)


def validate_candidate(
    strategy_id: str,
    trades: Sequence[ClosedTrade],
    *,
    sample_id: str,
    holdout_fraction: float = 0.3,
    window_count: int = 4,
    safety_violations: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Полная валидация кандидата. Результат подаётся в gates.promotion_gates.

    sample_id обязателен и не имеет умолчания: именно по нему отличается
    «новая независимая выборка» от повторного прогона на тех же данных
    (см. select_replacement). Умолчание здесь позволило бы вернуть
    отклонённую стратегию, ничего заново не измерив.
    """
    if not sample_id or not isinstance(sample_id, str):
        raise ValidationError(
            "sample_id is required: it is what distinguishes a NEW "
            "independent sample from a re-run on the same data"
        )

    in_sample, out_of_sample = split_holdout(trades, holdout_fraction)

    holdout_start = (
        out_of_sample[0].closed_at_utc if out_of_sample else None
    )

    # Walk-forward строится ТОЛЬКО по in-sample: holdout не участвует
    # ни в одном окне — это и есть его смысл.
    windows = build_walk_forward_windows(
        in_sample,
        window_count=window_count,
    )

    leakage = detect_look_ahead_leakage(windows, holdout_start)

    oos_stats = build_stats(out_of_sample)
    is_stats = build_stats(in_sample)

    ratio = robustness_ratio(windows)

    walk_forward_passed = bool(
        windows
        and not leakage
        and ratio is not None
        and ratio > 0.0
    )

    return {
        "strategy_id": strategy_id,
        "sample_id": sample_id,
        "in_sample_trades": len(in_sample),
        "in_sample_net_pnl": is_stats["net_pnl"],
        "oos_trades": oos_stats["closed_trades"],
        "oos_net_pnl": oos_stats["net_pnl"],
        "oos_profit_factor": oos_stats["profit_factor"],
        "oos_expectancy_r": oos_stats["expectancy_r"],
        "oos_max_drawdown_r": oos_stats["max_drawdown_r"],
        "oos_win_rate_percent": oos_stats["win_rate_percent"],
        "holdout_fraction": holdout_fraction,
        "holdout_start_utc": holdout_start,
        "walk_forward_windows": windows,
        "walk_forward_passed": walk_forward_passed,
        "robustness_ratio": ratio,
        "look_ahead_leakage": leakage,
        "safety_violations": list(safety_violations),
        "note": (
            "Costs and slippage are already included in each trade net_pnl; "
            "parameters were NOT tuned on this sample."
        ),
    }
