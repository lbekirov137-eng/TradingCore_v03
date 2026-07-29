"""
Плоский срез одного цикла paper-монитора.

ЗАЧЕМ. Журнальная запись вложенная и неоднородная: часть полей лежит в
pipeline.unified_market_context, часть — в pipeline.decision, часть — в
position_event.position, а запись об ошибке вообще не содержит pipeline.
Считать по такой форме статистику означает повторять одни и те же спуски
по ключам в каждом потребителе и расходиться в деталях.

Здесь запись приводится к ОДНОМУ плоскому словарю с фиксированным набором
ключей. Отсутствующее значение — это None, а не ноль: ноль означал бы
«измерено и равно нулю», и тогда средние и суммы молча поехали бы.

Модуль только читает. Он не может открыть позицию, изменить риск или
повлиять на сигнал.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence


# Значения, которые считаются «направление отсутствует».
_EMPTY_TEXT = {"", "NONE", "UNKNOWN", "NULL"}

FAILED_SAFELY_STATUSES = (
    "FAILED_SAFELY",
    "CANDLE_PROCESSING_FAILED_SAFELY",
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> float | None:
    """
    Число или None. Строки НЕ парсятся намеренно.

    Журнал пишется json.dump(default=str), поэтому нечисловой объект мог
    превратиться в строку. Принять такую строку за число значило бы
    протащить в отчёт значение, которое на самом деле не измерялось.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    number = float(value)

    return number if math.isfinite(number) else None


def _as_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = value.strip()

    if not cleaned or cleaned.upper() in _EMPTY_TEXT:
        return None

    return cleaned


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _as_text(value)

        if text is not None:
            return text

    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        number = _as_float(value)

        if number is not None:
            return number

    return None


def _lookup(sources: Sequence[dict[str, Any]], keys: Iterable[str]) -> Any:
    """Первое присутствующее значение по списку источников и ключей."""
    for source in sources:
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]

    return None


def build_observation(record: Any) -> dict[str, Any]:
    """
    Приводит журнальную запись к плоскому наблюдению за один цикл.

    Устойчиво к трём формам записи:
      - полный цикл с pipeline и position_event;
      - запись об ошибке (FAILED_SAFELY) без pipeline;
      - старые записи, где часть блоков ещё не писалась.

    Ни при какой форме не бросает исключение: отчёт о наблюдении не должен
    падать из-за того, что наблюдаемое оказалось неожиданным.
    """
    record = _as_dict(record)

    pipeline = _as_dict(record.get("pipeline"))
    unified = _as_dict(pipeline.get("unified_market_context"))
    review = _as_dict(pipeline.get("ai_opportunity_review"))
    decision_block = _as_dict(pipeline.get("decision"))
    paper_order = _as_dict(pipeline.get("paper_order"))
    strategy = _as_dict(pipeline.get("strategy"))
    selected_trade = _as_dict(strategy.get("selected_trade"))

    position_event = _as_dict(record.get("position_event"))
    position = _as_dict(
        position_event.get("position")
        if position_event.get("position") is not None
        else record.get("position")
    )

    status = _as_text(record.get("status"))
    failed_safely = status in FAILED_SAFELY_STATUSES if status else False

    # Сигнал: авторитетен исполненный ордер, затем выбор координатора и
    # только потом сигнал наследуемой EMA-стратегии. Тот же порядок, что и
    # в log_decision_line — расхождение здесь означало бы, что отчёт и
    # строка PAPER_DECISION описывают разные события.
    signal = _first_text(
        paper_order.get("signal"),
        selected_trade.get("signal"),
        strategy.get("signal"),
    )

    side = _first_text(
        paper_order.get("side"),
        position.get("side"),
        selected_trade.get("side"),
    )

    decision = _first_text(decision_block.get("decision"))

    event = _first_text(position_event.get("event"))

    # Причина отказа. NO_TRADE и FAILED_SAFELY — разные вещи и хранятся
    # раздельно, чтобы «мы не торговали, потому что нет сетапа» нельзя
    # было спутать с «мы не торговали, потому что упали».
    no_trade_reason = None

    if decision != "TRADE":
        no_trade_reason = _first_text(
            decision_block.get("reason"),
            position_event.get("reason"),
        )

    if no_trade_reason is None and event == "NO_POSITION_OPENED":
        no_trade_reason = _first_text(position_event.get("reason"))

    failure_reason = None

    if failed_safely:
        error_type = _as_text(record.get("error_type"))
        error_text = _as_text(record.get("error"))

        failure_reason = " ".join(
            part for part in (error_type, error_text) if part
        ) or status

    # Издержки известны только у ЗАКРЫТОЙ сделки: они считаются один раз в
    # момент закрытия. У открытой позиции их нет, и подставлять ноль
    # нельзя — это занизило бы суммарные комиссии.
    realized_pnl = _first_float(
        position_event.get("realized_pnl"),
        position.get("realized_pnl"),
    )

    net_pnl = _first_float(
        position_event.get("net_pnl"),
        position.get("net_pnl"),
    )

    gross_pnl = _first_float(
        position_event.get("gross_pnl"),
        position.get("gross_pnl"),
    )

    total_fees = _first_float(
        position_event.get("total_fees"),
        position.get("total_fees"),
    )

    slippage_cost = _first_float(
        position_event.get("slippage_cost"),
        position.get("slippage_cost"),
    )

    # real_order_sent: True хотя бы в одном месте — уже нарушение.
    # Поэтому берётся ИСТИНА ПО ЛЮБОМУ источнику, а не первое значение.
    real_order_sent = any(
        source.get("real_order_sent") is True
        for source in (record, pipeline, paper_order, position_event, position)
    )

    return {
        "recorded_at_utc": _first_text(record.get("recorded_at_utc")),
        "symbol": _first_text(
            record.get("symbol"),
            unified.get("symbol"),
        ),
        "timeframe": _first_text(
            record.get("timeframe"),
            unified.get("timeframe"),
        ),
        "market_price": _first_float(
            record.get("market_price"),
            unified.get("price"),
        ),
        "market_regime": _first_text(
            unified.get("market_regime"),
            _lookup([unified], ("regime", "primary_regime")),
        ),
        "signal": signal,
        "decision": decision,
        "side": side,
        "opportunity_score": _first_float(
            review.get("score"),
            _lookup([pipeline], ("opportunity_score",)),
        ),
        "atr_percent": _first_float(unified.get("atr_percent")),
        "relative_volume": _first_float(unified.get("relative_volume")),
        "position_event": event,
        "position_status": _first_text(position.get("status")),
        "entry": _first_float(
            position.get("entry"),
            paper_order.get("entry"),
        ),
        "stop": _first_float(
            position.get("stop"),
            paper_order.get("stop"),
        ),
        "take_profit_1": _first_float(
            position.get("take_profit_1"),
            paper_order.get("take_profit_1"),
        ),
        "take_profit_2": _first_float(
            position.get("take_profit_2"),
            paper_order.get("take_profit_2"),
        ),
        "exit_price": _first_float(
            position_event.get("exit_price"),
            position.get("exit_price"),
        ),
        "exit_reason": _first_text(
            position_event.get("exit_reason"),
            position.get("exit_reason"),
        ),
        "risk_amount": _first_float(position.get("risk_amount")),
        "quantity": _first_float(position.get("quantity")),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": _first_float(position.get("unrealized_pnl")),
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl if net_pnl is not None else realized_pnl,
        "total_fees": total_fees,
        "slippage_cost": slippage_cost,
        "real_order_sent": real_order_sent,
        "status": status,
        "failed_safely": failed_safely,
        "no_trade_reason": no_trade_reason,
        "failure_reason": failure_reason,
    }
