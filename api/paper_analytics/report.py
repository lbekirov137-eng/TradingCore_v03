"""
Агрегированная оценка paper-результатов и защитный статус.

ЗАЧЕМ. Строка PAPER_DECISION отвечает на вопрос «идут ли решения», но не
на вопрос «стоит ли этому доверять». Отдельные метрики (win rate, средний
R) по горстке сделок выглядят убедительно и при этом ничего не значат,
поэтому отчёт ОБЯЗАН показывать размер выборки рядом с любой метрикой и
явно помечать её как недостаточную.

Принципы:
  - неизвестное — это None, а не ноль; метрика без данных не печатается
    как 0.0, потому что ноль выглядит как измеренный результат;
  - net считается только по фактически закрытым сделкам и только после
    издержек: gross в оценку пригодности не входит;
  - защитный статус fail-closed: если признак нарушения обнаружен, статус
    ухудшается и НЕ может быть возвращён обратно агрегированием;
  - модуль ничего не исполняет и не останавливает — он только сообщает.
    Решение остановиться принимает человек.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

from api.paper_analytics.observation import build_observation


def journal_path() -> Path:
    """
    Путь к журналу цикла.

    Дублирует определение из paper_live_loop НАМЕРЕННО: импортировать
    paper_live_loop ради одной константы значит потянуть Bootstrap и
    провайдеров (~3 с) в веб-процесс, где торговый цикл может быть вообще
    выключен. Ровно по этой причине api/paper_monitor.py тоже откладывает
    свой импорт. Источник истины один и тот же — переменная PAPER_DATA_DIR.
    """
    return Path(os.getenv("PAPER_DATA_DIR", "data")) / "paper_runs.jsonl"


SAFE = "SAFE"
WARNING = "WARNING"
STOP = "STOP"

INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"

# Порог предварительной (не финальной) оценки. Обоснование в отчёте и в
# ответе пользователю: меньше этого числа доверительный интервал win rate
# шире самого win rate, и знак результата определяется одной сделкой.
MIN_CLOSED_TRADES_FOR_PRELIMINARY_READ = 30

# Сколько FAILED_SAFELY подряд считается «повторяется». Один сбой —
# транзиентная сетевая ошибка, это ожидаемо. Два подряд означают, что
# цикл не может обработать свечу, а не что рынок моргнул.
FAILED_SAFELY_STOP_STREAK = 2

# Серия убытков, после которой стоит присмотреться. Не STOP: сама по себе
# серия не означает поломки, для 40% win rate шесть подряд встречаются.
LOSS_STREAK_WARNING = 5

_CLOSING_EVENT = "POSITION_CLOSED"
_OPENING_EVENT = "POSITION_OPENED"

_TRADABLE_SIGNALS = {"BUY", "SELL"}
_SHORT_MARKERS = {"SHORT", "SELL"}


def load_records(
    journal_file: str | Path,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Читает JSONL-журнал, пропуская битые строки.

    Битая строка НЕ роняет отчёт: журнал пишется на каждой свече, и
    оборванная последняя строка после SIGKILL — нормальное состояние
    файла. Но пропуски считаются и попадают в отчёт, чтобы «файл
    частично нечитаем» не выглядело как «сделок не было».
    """
    path = Path(journal_file)

    if not path.exists():
        return []

    records: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                records.append({"__unreadable__": True})
                continue

            records.append(parsed if isinstance(parsed, dict) else {})

    if limit is not None and limit > 0:
        records = records[-limit:]

    return records


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None

    return round(value, digits)


def _risk_for(observation: dict[str, Any]) -> float | None:
    """
    Риск сделки в деньгах — знаменатель для R.

    Приоритет у записанного risk_amount. Если его нет, риск
    восстанавливается из уровней: |entry - stop| * quantity. Это ровно то
    же определение, по которому уровни и ставились, поэтому подмены
    семантики нет.
    """
    risk = observation.get("risk_amount")

    if isinstance(risk, (int, float)) and risk > 0:
        return float(risk)

    entry = observation.get("entry")
    stop = observation.get("stop")
    quantity = observation.get("quantity")

    if None in (entry, stop, quantity):
        return None

    risk = abs(float(entry) - float(stop)) * float(quantity)

    return risk if risk > 0 else None


def _max_drawdown(pnls: list[float]) -> float | None:
    """
    Максимальная просадка кривой капитала по закрытым сделкам.

    Возвращается ПОЛОЖИТЕЛЬНОЙ величиной падения от пика. Без сделок —
    None, а не 0.0: «просадки не было» и «мерить нечего» это разное.
    """
    if not pnls:
        return None

    equity = 0.0
    peak = 0.0
    drawdown = 0.0

    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    return drawdown


def _max_loss_streak(pnls: list[float]) -> int:
    streak = 0
    longest = 0

    for pnl in pnls:
        if pnl < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    return longest


def _failed_safely_streak(observations: list[dict[str, Any]]) -> int:
    streak = 0
    longest = 0

    for observation in observations:
        if observation.get("failed_safely"):
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    return longest


def _contradictions(observations: list[dict[str, Any]]) -> list[str]:
    """
    Противоречия между сигналом и решением.

    Оба направления опасны и означают разное:
      - TRADE без торгового сигнала: сделка возникла не из стратегии;
      - NO_TRADE с открытой позицией: отказ не остановил исполнение.
    """
    found: list[str] = []

    for observation in observations:
        decision = observation.get("decision")
        signal = observation.get("signal")
        event = observation.get("position_event")
        utc = observation.get("recorded_at_utc")

        if decision == "TRADE" and signal not in _TRADABLE_SIGNALS:
            found.append(
                f"{utc}: decision=TRADE but signal={signal!r}"
            )

        if decision == "NO_TRADE" and event == _OPENING_EVENT:
            found.append(
                f"{utc}: decision=NO_TRADE but position was OPENED"
            )

    return found


def build_report(
    records: Iterable[Any],
    min_closed_trades: int = MIN_CLOSED_TRADES_FOR_PRELIMINARY_READ,
) -> dict[str, Any]:
    """
    Сводит журнальные записи в отчёт с метриками и защитным статусом.

    Никогда не бросает исключение на неожиданной записи: неизвестная форма
    считается наблюдением без данных, а не поводом уронить эндпоинт.
    """
    raw = list(records)

    unreadable = sum(
        1 for item in raw if isinstance(item, dict) and item.get("__unreadable__")
    )

    observations = [
        build_observation(item)
        for item in raw
        if not (isinstance(item, dict) and item.get("__unreadable__"))
    ]

    cycles = len(observations)

    trade_decisions = sum(
        1 for item in observations if item.get("decision") == "TRADE"
    )
    no_trade_decisions = sum(
        1 for item in observations if item.get("decision") == "NO_TRADE"
    )

    opened = [
        item for item in observations
        if item.get("position_event") == _OPENING_EVENT
    ]
    closed = [
        item for item in observations
        if item.get("position_event") == _CLOSING_EVENT
    ]

    # net_pnl закрытой сделки == realized_pnl (см. position_manager):
    # фактический результат ПОСЛЕ издержек.
    net_pnls = [
        float(item["net_pnl"])
        for item in closed
        if item.get("net_pnl") is not None
    ]

    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]
    breakeven = [pnl for pnl in net_pnls if pnl == 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    profit_factor: float | None = None

    if net_pnls:
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            # Убытков нет вовсе. Бесконечность здесь не информативна и не
            # сериализуется в JSON — честнее сказать «не определён».
            profit_factor = None

    r_multiples: list[float] = []

    for item in closed:
        risk = _risk_for(item)
        net = item.get("net_pnl")

        if risk and net is not None:
            r_multiples.append(float(net) / risk)

    total_fees = sum(
        float(item["total_fees"])
        for item in closed
        if item.get("total_fees") is not None
    )
    total_slippage = sum(
        float(item["slippage_cost"])
        for item in closed
        if item.get("slippage_cost") is not None
    )
    gross_total = sum(
        float(item["gross_pnl"])
        for item in closed
        if item.get("gross_pnl") is not None
    )

    failed_safely = [item for item in observations if item.get("failed_safely")]
    failed_streak = _failed_safely_streak(observations)

    real_order_breaches = [
        item for item in observations if item.get("real_order_sent") is True
    ]

    short_sightings = [
        item for item in observations
        if (item.get("side") or "").upper() in _SHORT_MARKERS
        or (item.get("signal") or "").upper() == "SELL"
    ]

    contradictions = _contradictions(observations)

    no_trade_reasons: dict[str, int] = {}

    for item in observations:
        reason = item.get("no_trade_reason")

        if reason:
            no_trade_reasons[reason] = no_trade_reasons.get(reason, 0) + 1

    failure_reasons: dict[str, int] = {}

    for item in failed_safely:
        reason = item.get("failure_reason") or "UNKNOWN"
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    # ------------------------------------------------------------ статус
    stop_reasons: list[str] = []
    warning_reasons: list[str] = []

    if real_order_breaches:
        stop_reasons.append(
            f"real_order_sent=True in {len(real_order_breaches)} record(s)"
        )

    if short_sightings:
        stop_reasons.append(
            f"SHORT/SELL direction observed in {len(short_sightings)} record(s)"
        )

    if failed_streak >= FAILED_SAFELY_STOP_STREAK:
        stop_reasons.append(
            f"FAILED_SAFELY repeated {failed_streak} times in a row"
        )

    if contradictions:
        stop_reasons.append(
            f"signal/decision contradiction in {len(contradictions)} record(s)"
        )

    if failed_safely and failed_streak < FAILED_SAFELY_STOP_STREAK:
        warning_reasons.append(
            f"{len(failed_safely)} isolated FAILED_SAFELY record(s)"
        )

    loss_streak = _max_loss_streak(net_pnls)

    if loss_streak >= LOSS_STREAK_WARNING:
        warning_reasons.append(
            f"losing streak of {loss_streak} closed trades"
        )

    if unreadable:
        warning_reasons.append(
            f"{unreadable} unreadable journal line(s)"
        )

    if stop_reasons:
        status = STOP
    elif warning_reasons:
        status = WARNING
    else:
        status = SAFE

    closed_count = len(closed)
    sufficient = closed_count >= min_closed_trades

    win_rate = (
        (len(wins) / closed_count * 100.0) if closed_count else None
    )
    average_r = (
        (sum(r_multiples) / len(r_multiples)) if r_multiples else None
    )

    return {
        "schema_version": "PAPER_PERFORMANCE_V1",
        "mode": "PAPER",
        "real_orders_enabled": False,
        "safety_status": status,
        "stop_reasons": stop_reasons,
        "warning_reasons": warning_reasons,
        "sample": {
            "sufficient": sufficient,
            "verdict": None if sufficient else INSUFFICIENT_SAMPLE,
            "closed_trades": closed_count,
            "required_closed_trades": min_closed_trades,
            "missing_closed_trades": max(0, min_closed_trades - closed_count),
        },
        "cycles": {
            "total": cycles,
            "trade_decisions": trade_decisions,
            "no_trade_decisions": no_trade_decisions,
            "unreadable_journal_lines": unreadable,
        },
        "trades": {
            "opened": len(opened),
            "closed": closed_count,
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "win_rate_percent": _round(win_rate),
            "net_pnl": _round(sum(net_pnls), 8),
            "gross_pnl": _round(gross_total, 8),
            "total_fees": _round(total_fees, 8),
            "slippage_cost": _round(total_slippage, 8),
            "profit_factor": _round(profit_factor, 3),
            "average_r": _round(average_r, 3),
            "max_drawdown": _round(_max_drawdown(net_pnls), 8),
            "max_loss_streak": loss_streak,
        },
        "safety": {
            "real_order_sent_count": len(real_order_breaches),
            "short_direction_count": len(short_sightings),
            "failed_safely_count": len(failed_safely),
            "failed_safely_max_streak": failed_streak,
            "contradictions": contradictions[:20],
            "contradiction_count": len(contradictions),
        },
        "reasons": {
            "no_trade": dict(
                sorted(
                    no_trade_reasons.items(),
                    key=lambda pair: pair[1],
                    reverse=True,
                )[:15]
            ),
            "failures": dict(
                sorted(
                    failure_reasons.items(),
                    key=lambda pair: pair[1],
                    reverse=True,
                )[:15]
            ),
        },
        "last_cycle": observations[-1] if observations else None,
    }


def _format(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"

    return f"{value}{suffix}"


def render_report_text(
    report: dict[str, Any],
    title: str = "DAILY PAPER REPORT",
) -> str:
    """
    Человекочитаемый отчёт для Deploy Logs.

    Формат рассчитан на чтение в потоке логов Railway: фиксированная
    ширина, устойчивые префиксы, размер выборки СРАЗУ под метриками, а не
    в конце — иначе win rate прочитают раньше, чем узнают, что он посчитан
    по трём сделкам.
    """
    cycles = report.get("cycles", {})
    trades = report.get("trades", {})
    sample = report.get("sample", {})
    safety = report.get("safety", {})
    reasons = report.get("reasons", {})

    line = "=" * 68

    out: list[str] = [
        line,
        f"{title}   [mode=PAPER real_orders=False]",
        line,
        f"STATUS: {report.get('safety_status')}",
    ]

    for reason in report.get("stop_reasons", []):
        out.append(f"  STOP:    {reason}")

    for reason in report.get("warning_reasons", []):
        out.append(f"  WARNING: {reason}")

    out.append("")
    out.append(
        f"CYCLES: {cycles.get('total')} "
        f"(TRADE={cycles.get('trade_decisions')} "
        f"NO_TRADE={cycles.get('no_trade_decisions')})"
    )
    out.append(
        f"TRADES: opened={trades.get('opened')} "
        f"closed={trades.get('closed')} "
        f"wins={trades.get('wins')} losses={trades.get('losses')}"
    )

    if not sample.get("sufficient"):
        # Печатается ДО метрик: метрики ниже читать как ориентир нельзя.
        out.append("")
        out.append(
            f"{INSUFFICIENT_SAMPLE}: "
            f"closed_trades={sample.get('closed_trades')} "
            f"required={sample.get('required_closed_trades')} "
            f"missing={sample.get('missing_closed_trades')}"
        )

    out.append("")
    out.append(
        f"NET PnL (after costs): {_format(trades.get('net_pnl'))}"
    )
    out.append(
        f"  gross={_format(trades.get('gross_pnl'))} "
        f"fees={_format(trades.get('total_fees'))} "
        f"slippage={_format(trades.get('slippage_cost'))}"
    )
    out.append(
        f"WIN RATE:      {_format(trades.get('win_rate_percent'), '%')}"
    )
    out.append(
        f"PROFIT FACTOR: {_format(trades.get('profit_factor'))}"
    )
    out.append(
        f"AVERAGE R:     {_format(trades.get('average_r'))}"
    )
    out.append(
        f"MAX DRAWDOWN:  {_format(trades.get('max_drawdown'))}"
    )
    out.append(
        f"LOSS STREAK:   {_format(trades.get('max_loss_streak'))}"
    )

    out.append("")
    out.append(
        "GUARDS: "
        f"real_order_sent={safety.get('real_order_sent_count')} "
        f"short={safety.get('short_direction_count')} "
        f"failed_safely={safety.get('failed_safely_count')}"
        f" (max streak {safety.get('failed_safely_max_streak')}) "
        f"contradictions={safety.get('contradiction_count')}"
    )

    no_trade_reasons = reasons.get("no_trade") or {}

    if no_trade_reasons:
        out.append("")
        out.append("TOP NO_TRADE REASONS:")

        for reason, count in list(no_trade_reasons.items())[:5]:
            out.append(f"  {count:>5}x {reason}")

    failure_reasons = reasons.get("failures") or {}

    if failure_reasons:
        out.append("")
        out.append("FAILURES:")

        for reason, count in list(failure_reasons.items())[:5]:
            out.append(f"  {count:>5}x {reason}")

    out.append(line)

    return "\n".join(out)
