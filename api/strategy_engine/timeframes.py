"""
Детерминированная агрегация таймфреймов с провенансом.

ПОЧЕМУ НЕ ПРОСТО «каждые N свечей». Группировка по счётчику молча
рассыпается на реальных данных: один пропуск сдвигает все последующие
границы, и 1H-свеча начинает собираться из кусков разных часов. Здесь
бакет определяется ВРЕМЕНЕМ (floor по границе таймфрейма), а не позицией,
поэтому пропуск ломает ровно один бакет и не заражает соседние.

Второй инвариант: НЕПОЛНЫЙ бакет отбрасывается. Свеча, собранная из 7
пятиминуток вместо 12, не является закрытой часовой свечой; принять её —
значит принимать решение по незакрытым данным.

Провенанс возвращается вместе с результатом: сколько бакетов отброшено и
почему. Без этого «1H свечей меньше, чем ожидалось» невозможно отличить от
ошибки агрегации.
"""

from __future__ import annotations

from typing import Any, Sequence

from api.strategy_engine.strategies.contracts import Candle


MINUTE_MS = 60 * 1000

TIMEFRAME_MS = {
    "5m": 5 * MINUTE_MS,
    "15m": 15 * MINUTE_MS,
    "1H": 60 * MINUTE_MS,
    "4H": 4 * 60 * MINUTE_MS,
}


def aggregate_by_time(
    candles: Sequence[Candle],
    source_timeframe: str,
    target_timeframe: str,
) -> dict[str, Any]:
    """
    Агрегирует свечи в целевой таймфрейм по ВРЕМЕННЫМ границам.

    Бакет определяется как floor(open_time_ms / target_ms), поэтому
    границы совпадают с календарными часами UTC независимо от того, с
    какой свечи начинается история. Это же обеспечивает согласованность
    таймзоны: всё считается в UTC, локального времени здесь нет вообще.
    """
    source_ms = TIMEFRAME_MS.get(source_timeframe)
    target_ms = TIMEFRAME_MS.get(target_timeframe)

    if source_ms is None or target_ms is None:
        raise ValueError(
            f"unknown timeframe: {source_timeframe} -> {target_timeframe}"
        )

    if target_ms % source_ms != 0 or target_ms <= source_ms:
        raise ValueError(
            f"{target_timeframe} is not a whole multiple of {source_timeframe}"
        )

    expected_per_bucket = target_ms // source_ms

    buckets: dict[int, list[Candle]] = {}
    duplicates = 0
    out_of_order = 0

    previous_time = None
    seen: set[int] = set()

    for candle in candles:
        if previous_time is not None and candle.open_time_ms < previous_time:
            out_of_order += 1

        previous_time = candle.open_time_ms

        if candle.open_time_ms in seen:
            duplicates += 1
            # Дубликат ОТБРАСЫВАЕТСЯ, а не суммируется: сложение объёмов
            # двух копий одной свечи исказило бы VWAP и relative volume.
            continue

        seen.add(candle.open_time_ms)

        bucket_start = (candle.open_time_ms // target_ms) * target_ms
        buckets.setdefault(bucket_start, []).append(candle)

    aggregated: list[Candle] = []
    incomplete = 0

    for bucket_start in sorted(buckets):
        block = buckets[bucket_start]

        if len(block) != expected_per_bucket:
            # Неполный бакет — не закрытая свеча целевого таймфрейма.
            incomplete += 1
            continue

        block.sort(key=lambda item: item.open_time_ms)

        aggregated.append(
            Candle(
                open_time_ms=bucket_start,
                open=block[0].open,
                high=max(item.high for item in block),
                low=min(item.low for item in block),
                close=block[-1].close,
                volume=sum(item.volume for item in block),
            )
        )

    gaps = _count_gaps(aggregated, target_ms)

    return {
        "candles": aggregated,
        "provenance": {
            "source_timeframe": source_timeframe,
            "target_timeframe": target_timeframe,
            "source_candles": len(candles),
            "expected_per_bucket": expected_per_bucket,
            "buckets_seen": len(buckets),
            "candles_produced": len(aggregated),
            "incomplete_buckets_dropped": incomplete,
            "duplicate_timestamps_dropped": duplicates,
            "out_of_order_source_candles": out_of_order,
            "gaps": gaps,
            "first_open_time_ms": (
                aggregated[0].open_time_ms if aggregated else None
            ),
            "last_open_time_ms": (
                aggregated[-1].open_time_ms if aggregated else None
            ),
        },
    }


def _count_gaps(candles: Sequence[Candle], step_ms: int) -> int:
    """Число разрывов в последовательности целевых свечей."""
    gaps = 0

    for position in range(1, len(candles)):
        delta = candles[position].open_time_ms - candles[position - 1].open_time_ms

        if delta != step_ms:
            gaps += 1

    return gaps


def utc_iso(open_time_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(
        open_time_ms / 1000.0, tz=timezone.utc
    ).isoformat()


def align_context(
    execution_candle: Candle,
    context_candles: Sequence[Candle],
    context_timeframe: str = "4H",
) -> Candle | None:
    """
    Последняя ЗАКРЫТАЯ свеча старшего таймфрейма для данной 1H свечи.

    КРИТИЧНО ДЛЯ ОТСУТСТВИЯ УТЕЧКИ. 4H свеча считается доступной только
    когда она полностью закрылась, то есть её open_time + 4H <= open_time
    текущей 1H свечи. Обычная ошибка — взять 4H свечу, ВНУТРИ которой
    находится текущий час: она ещё не закрыта, и её high/low/close
    содержат будущее относительно момента решения.

    Возвращает None, если закрытого контекста ещё нет.
    """
    context_ms = TIMEFRAME_MS[context_timeframe]

    deadline = execution_candle.open_time_ms

    latest: Candle | None = None

    for candle in context_candles:
        if candle.open_time_ms + context_ms <= deadline:
            latest = candle
        else:
            break

    return latest
