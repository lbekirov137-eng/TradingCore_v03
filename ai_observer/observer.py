from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RAILWAY_JOURNAL = Path("/app/data/paper_runs.jsonl")
LOCAL_JOURNAL = Path("data/paper_runs.jsonl")

STARTING_BALANCE_USD = float(
    os.getenv("PAPER_STARTING_BALANCE", "1000")
)


@dataclass
class ObserverSummary:
    total_records: int = 0
    no_position_opened: int = 0
    position_opened: int = 0
    position_remains_open: int = 0
    position_closed: int = 0
    tp1_reached: int = 0

    profitable: int = 0
    losing: int = 0
    breakeven: int = 0

    realized_pnl: float = 0.0
    maximum_loss_streak: int = 0
    current_loss_streak: int = 0

    invalid_lines: int = 0
    real_orders_sent: int = 0


def resolve_journal_path() -> Path:
    custom_path = os.getenv("PAPER_JOURNAL_PATH")

    if custom_path:
        return Path(custom_path)

    if RAILWAY_JOURNAL.exists():
        return RAILWAY_JOURNAL

    return LOCAL_JOURNAL


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_records(
    journal_path: Path,
) -> tuple[list[dict[str, Any]], int]:
    if not journal_path.exists():
        raise FileNotFoundError(
            f"Журнал не найден: {journal_path}"
        )

    records: list[dict[str, Any]] = []
    invalid_lines = 0

    with journal_path.open("r", encoding="utf-8-sig") as file:
        for raw_line in file:
            line = raw_line.strip()

            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue

            if isinstance(record, dict):
                records.append(record)
            else:
                invalid_lines += 1

    return records, invalid_lines


def get_position_event(
    record: dict[str, Any],
) -> dict[str, Any]:
    value = record.get("position_event")

    if isinstance(value, dict):
        return value

    return {}


def get_event_name(record: dict[str, Any]) -> str:
    position_event = get_position_event(record)
    event = position_event.get("event")

    return str(event or "").strip().upper()


def get_realized_pnl(record: dict[str, Any]) -> float:
    position_event = get_position_event(record)

    if "realized_pnl" in position_event:
        return safe_float(position_event.get("realized_pnl"))

    position = position_event.get("position")

    if isinstance(position, dict):
        return safe_float(position.get("realized_pnl"))

    return 0.0


def real_order_was_sent(record: dict[str, Any]) -> bool:
    if record.get("real_order_sent") is True:
        return True

    position_event = get_position_event(record)

    if position_event.get("real_order_sent") is True:
        return True

    position = position_event.get("position")

    if isinstance(position, dict):
        return position.get("real_order_sent") is True

    return False


def analyze_records(
    records: list[dict[str, Any]],
    invalid_lines: int,
) -> tuple[ObserverSummary, list[dict[str, Any]]]:
    summary = ObserverSummary(
        total_records=len(records),
        invalid_lines=invalid_lines,
    )

    closed_trades: list[dict[str, Any]] = []

    for record in records:
        event = get_event_name(record)

        if real_order_was_sent(record):
            summary.real_orders_sent += 1

        if event == "NO_POSITION_OPENED":
            summary.no_position_opened += 1
            continue

        if event == "POSITION_OPENED":
            summary.position_opened += 1
            continue

        if event == "POSITION_REMAINS_OPEN":
            summary.position_remains_open += 1
            continue

        if event == "TAKE_PROFIT_1_REACHED":
            summary.tp1_reached += 1
            continue

        if event != "POSITION_CLOSED":
            continue

        summary.position_closed += 1

        position_event = get_position_event(record)
        position = position_event.get("position")

        if not isinstance(position, dict):
            position = {}

        pnl = get_realized_pnl(record)

        closed_trade = {
            "recorded_at_utc": record.get("recorded_at_utc"),
            "symbol": position.get(
                "symbol",
                record.get("symbol", "UNKNOWN"),
            ),
            "side": position.get("side", "UNKNOWN"),
            "entry": safe_float(position.get("entry")),
            "exit_price": safe_float(
                position_event.get(
                    "exit_price",
                    position.get("exit_price"),
                )
            ),
            "exit_reason": position_event.get(
                "exit_reason",
                position.get("exit_reason", "UNKNOWN"),
            ),
            "pnl": pnl,
        }

        closed_trades.append(closed_trade)
        summary.realized_pnl += pnl

        if pnl > 0:
            summary.profitable += 1
            summary.current_loss_streak = 0

        elif pnl < 0:
            summary.losing += 1
            summary.current_loss_streak += 1

            summary.maximum_loss_streak = max(
                summary.maximum_loss_streak,
                summary.current_loss_streak,
            )

        else:
            summary.breakeven += 1
            summary.current_loss_streak = 0

    return summary, closed_trades


def find_current_open_position(
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for record in reversed(records):
        position_event = get_position_event(record)
        position = position_event.get("position")

        if not isinstance(position, dict):
            continue

        status = str(position.get("status", "")).upper()

        if status == "OPEN":
            return position

        if status == "CLOSED":
            return None

    return None


def build_assessment(summary: ObserverSummary) -> list[str]:
    assessment: list[str] = []

    if summary.position_closed == 0:
        assessment.append(
            "Недостаточно закрытых сделок для оценки стратегии."
        )
        return assessment

    win_rate = (
        summary.profitable
        / summary.position_closed
        * 100
    )

    if summary.realized_pnl < 0:
        assessment.append(
            "Реализованный результат стратегии отрицательный."
        )
    elif summary.realized_pnl > 0:
        assessment.append(
            "Реализованный результат стратегии положительный."
        )
    else:
        assessment.append(
            "Реализованный результат стратегии около нуля."
        )

    if win_rate < 35:
        assessment.append(
            "Доля прибыльных сделок низкая. "
            "Нужно улучшать фильтрацию входов."
        )
    elif win_rate < 50:
        assessment.append(
            "Доля прибыльных сделок средняя. "
            "Нужно проверить соотношение прибыли и риска."
        )
    else:
        assessment.append(
            "Доля прибыльных сделок выше 50%."
        )

    if summary.maximum_loss_streak >= 4:
        assessment.append(
            "Обнаружена серия из "
            f"{summary.maximum_loss_streak} "
            "убыточных сделок подряд."
        )

    if summary.real_orders_sent > 0:
        assessment.append(
            "ВНИМАНИЕ: журнал показывает отправку "
            "реальных ордеров."
        )
    else:
        assessment.append(
            "Реальные ордера не отправлялись."
        )

    if (
        summary.realized_pnl <= 0
        or summary.maximum_loss_streak >= 4
    ):
        assessment.append(
            "Рекомендация: продолжать PAPER MODE. "
            "Реальные деньги не включать."
        )
    else:
        assessment.append(
            "Рекомендация: продолжить PAPER-тест "
            "до накопления достаточной статистики."
        )

    return assessment


def format_open_position(
    position: dict[str, Any] | None,
) -> list[str]:
    if position is None:
        return [
            "ОТКРЫТАЯ ПОЗИЦИЯ:",
            "Нет открытой позиции.",
        ]

    entry = safe_float(position.get("entry"))
    market_price = safe_float(
        position.get("last_market_price")
    )
    unrealized_pnl = safe_float(
        position.get("unrealized_pnl")
    )

    return [
        "ОТКРЫТАЯ ПОЗИЦИЯ:",
        (
            f"{position.get('side', 'UNKNOWN')} "
            f"{position.get('symbol', 'UNKNOWN')}"
        ),
        f"Вход: ${entry:.2f}",
        f"Текущая цена: ${market_price:.2f}",
        f"Плавающий PNL: {unrealized_pnl:+.2f} USD",
    ]


def format_recent_trades(
    closed_trades: list[dict[str, Any]],
    limit: int = 10,
) -> list[str]:
    lines = [
        "",
        f"ПОСЛЕДНИЕ {min(limit, len(closed_trades))} "
        "ЗАКРЫТЫХ СДЕЛОК:",
    ]

    if not closed_trades:
        lines.append("Закрытых сделок нет.")
        return lines

    for number, trade in enumerate(
        closed_trades[-limit:],
        start=1,
    ):
        lines.append(
            f"{number}. "
            f"{trade['symbol']} "
            f"{trade['exit_reason']} "
            f"{trade['pnl']:+.2f} USD"
        )

    return lines


def build_report(
    journal_path: Path,
    records: list[dict[str, Any]],
    summary: ObserverSummary,
    closed_trades: list[dict[str, Any]],
) -> str:
    first_record_time = (
        records[0].get("recorded_at_utc")
        if records
        else "нет данных"
    )

    last_record_time = (
        records[-1].get("recorded_at_utc")
        if records
        else "нет данных"
    )

    current_balance = (
        STARTING_BALANCE_USD
        + summary.realized_pnl
    )

    win_rate = (
        summary.profitable
        / summary.position_closed
        * 100
        if summary.position_closed
        else 0.0
    )

    current_position = find_current_open_position(records)
    assessment = build_assessment(summary)

    lines = [
        "🤖 AI OBSERVER — PAPER REPORT",
        "",
        f"Журнал: {journal_path}",
        f"Первая запись: {first_record_time}",
        f"Последняя запись: {last_record_time}",
        "",
        f"Всего записей: {summary.total_records}",
        (
            "Без открытия сделки: "
            f"{summary.no_position_opened}"
        ),
        f"Открыто позиций: {summary.position_opened}",
        (
            "Проверок открытой позиции: "
            f"{summary.position_remains_open}"
        ),
        f"Закрыто позиций: {summary.position_closed}",
        f"TP1 достигнут: {summary.tp1_reached}",
        f"Прибыльных: {summary.profitable}",
        f"Убыточных: {summary.losing}",
        f"Безубыточных: {summary.breakeven}",
        f"Win rate: {win_rate:.1f}%",
        (
            "Максимальная серия убытков: "
            f"{summary.maximum_loss_streak}"
        ),
        (
            "Повреждённых строк: "
            f"{summary.invalid_lines}"
        ),
        "",
        "ПРОСТАЯ ФОРМУЛА:",
        f"${STARTING_BALANCE_USD:.2f}",
        f"{summary.realized_pnl:+.2f} USD PNL",
        "--------------------",
        f"${current_balance:.2f} закрытый баланс",
        "",
    ]

    lines.extend(format_open_position(current_position))
    lines.extend(format_recent_trades(closed_trades))

    lines.extend(
        [
            "",
            "ОЦЕНКА:",
        ]
    )

    for item in assessment:
        lines.append(f"• {item}")

    lines.extend(
        [
            "",
            (
                "REAL ORDERS SENT: "
                f"{summary.real_orders_sent}"
            ),
            "REAL ORDERS: DISABLED",
            (
                "Отчёт создан: "
                f"{datetime.now(timezone.utc).isoformat()}"
            ),
        ]
    )

    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print(
            "\nTelegram пропущен: "
            "TELEGRAM_BOT_TOKEN или "
            "TELEGRAM_CHAT_ID отсутствует."
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{token}/sendMessage"
    )

    chunks = [
        text[index:index + 3900]
        for index in range(0, len(text), 3900)
    ]

    try:
        for chunk in chunks:
            payload = urllib.parse.urlencode(
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                }
            ).encode("utf-8")

            request = urllib.request.Request(
                url=url,
                data=payload,
                method="POST",
            )

            with urllib.request.urlopen(
                request,
                timeout=20,
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

            if not result.get("ok"):
                return False

        return True

    except Exception as exc:
        print(
            "\nTelegram error: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


def main() -> int:
    journal_path = resolve_journal_path()

    try:
        records, invalid_lines = load_records(
            journal_path
        )

    except Exception as exc:
        print(
            "AI OBSERVER ERROR: "
            f"{type(exc).__name__}: {exc}"
        )
        return 1

    if not records:
        print("AI OBSERVER ERROR: журнал пуст.")
        return 1

    summary, closed_trades = analyze_records(
        records=records,
        invalid_lines=invalid_lines,
    )

    report = build_report(
        journal_path=journal_path,
        records=records,
        summary=summary,
        closed_trades=closed_trades,
    )

    print()
    print("=" * 70)
    print(report)
    print("=" * 70)

    telegram_sent = send_telegram_message(report)

    print()
    print(f"TELEGRAM REPORT SENT: {telegram_sent}")
    print("REAL ORDER SENT: False")

    return 0


if __name__ == "__main__":
    sys.exit(main())