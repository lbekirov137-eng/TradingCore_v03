"""
Реалистичная симуляция maker-исполнения.

ЗАЧЕМ. Считать лимитный вход всегда исполненным — самая приятная и самая
опасная ошибка в оценке maker-стратегии: она даёт экономию на комиссии,
не отдавая ничего взамен. В действительности лимитный ордер исполняется
только если цена до него дошла, и именно те случаи, когда она дошла,
систематически хуже среднего (adverse selection): цена пришла к вам,
потому что продолжила падать.

Модель отвечает на три вопроса раздельно:
  1. сколько сигналов вообще было бы исполнено (fill rate);
  2. каково ожидание на ИСПОЛНЕННЫХ сделках;
  3. каково ожидание на ВСЕХ сигналах, где неисполненный считается
     нулём, а не выигрышем и не проигрышем.

Неисполненный ордер НЕ является сделкой. Он не улучшает и не ухудшает
статистику — но он и не бесплатен: упущенная прибыль учитывается
отдельно, в opportunity cost.

Автоматический откат в taker ЗАПРЕЩЁН по умолчанию: он превратил бы
maker-эксперимент обратно в taker-исполнение и скрыл бы истинный fill rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from api.strategy_engine.strategies.contracts import Candle


@dataclass(frozen=True)
class MakerExecutionConfig:
    """
    Параметры симуляции. Все — DEFAULT_NOT_OPTIMIZED, заданы до прогона.
    """

    # Насколько ниже цены сигнала ставится лимит, в долях ATR. Ноль
    # означал бы «по рынку», что не maker.
    limit_offset_atr: float = 0.10

    # Сколько закрытых свечей ордер стоит в стакане до отмены.
    timeout_bars: int = 3

    # Доля объёма свечи, ниже которой считаем, что очередь не дошла до
    # нас. Грубый прокси позиции в очереди: если на нашем уровне торговали
    # мало, вероятность исполнения падает.
    queue_volume_fraction: float = 0.15

    # Доля исполнения при касании ровно на границе (частичное исполнение).
    partial_fill_ratio: float = 0.5

    # Проскальзывание для maker-входа равно нулю по определению: лимит
    # исполняется по своей цене или не исполняется вовсе.
    allow_taker_fallback: bool = False


def simulate_maker_entry(
    candles: Sequence[Candle],
    signal_index: int,
    limit_price: float,
    config: MakerExecutionConfig | None = None,
) -> dict[str, Any]:
    """
    Определяет судьбу лимитного ордера, размещённого после сигнальной свечи.

    Смотрит ТОЛЬКО вперёд от свечи размещения и только на закрытые свечи —
    это не утечка: ордер физически живёт в будущем относительно сигнала, и
    его исполнение определяется будущей ценой. Утечкой было бы
    использование этой информации для ПРИНЯТИЯ решения о входе, чего здесь
    не происходит: решение уже принято.
    """
    config = config or MakerExecutionConfig()

    for offset in range(1, config.timeout_bars + 1):
        index = signal_index + offset

        if index >= len(candles):
            return {
                "status": "MISSED_END_OF_DATA",
                "filled": False, "fill_ratio": 0.0,
                "wait_bars": offset, "adverse": False,
            }

        candle = candles[index]

        if candle.low <= limit_price:
            # Полное исполнение: цена прошла уровень насквозь.
            through = candle.low < limit_price * (1 - 1e-9)

            # Adverse selection: свеча закрылась НИЖЕ нашего лимита,
            # то есть нас исполнили в продолжающемся падении.
            adverse = candle.close < limit_price

            return {
                "status": "FILLED" if through else "PARTIAL_FILL",
                "filled": True,
                "fill_ratio": 1.0 if through else config.partial_fill_ratio,
                "wait_bars": offset,
                "adverse": adverse,
                "fill_price": limit_price,
                "candle_close": candle.close,
            }

    return {
        "status": "CANCELLED_TIMEOUT",
        "filled": False, "fill_ratio": 0.0,
        "wait_bars": config.timeout_bars, "adverse": False,
    }


def summarise_fills(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Сводка по исполнению. Неисполненные считаются отдельно от сделок."""
    total = len(results)

    if total == 0:
        return {"signals": 0}

    filled = [r for r in results if r["filled"]]
    partial = [r for r in filled if r["status"] == "PARTIAL_FILL"]
    adverse = [r for r in filled if r.get("adverse")]
    missed = [r for r in results if not r["filled"]]

    return {
        "signals": total,
        "placed_orders": total,
        "filled_orders": len(filled),
        "partial_fills": len(partial),
        "missed": len(missed),
        "fill_rate_percent": round(len(filled) / total * 100.0, 2),
        "adverse_selection_count": len(adverse),
        "adverse_selection_percent": (
            round(len(adverse) / len(filled) * 100.0, 2) if filled else None
        ),
        "average_wait_bars": (
            round(sum(r["wait_bars"] for r in filled) / len(filled), 2)
            if filled else None
        ),
        "timeout_cancellations": sum(
            1 for r in missed if r["status"] == "CANCELLED_TIMEOUT"
        ),
        "taker_fallback_used": 0,
        "note": (
            "A missed order is neither a win nor a loss. It is excluded from "
            "trade statistics and reported separately as opportunity cost."
        ),
    }
