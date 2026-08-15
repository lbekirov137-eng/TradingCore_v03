#!/usr/bin/env python3
"""Performance wrapper for Historical Accelerator V1.

Does not alter protocol, signals, trade geometry, validation, or holdout. It only
replaces repeated time-series lookup/statistics with mathematically identical
cached O(log n)/O(1) versions before calling historical_accelerator.main().
"""
from __future__ import annotations

import bisect
import math
from typing import Any

import historical_accelerator as engine
import historical_accelerator_protocol as protocol

# id(list) -> (length, times, prefix_sum, prefix_sq_sum)
_CACHE: dict[int, tuple[int, list[int], list[float], list[float]]] = {}


def _meta(rows: list[engine.Sample]) -> tuple[list[int], list[float], list[float]]:
    key = id(rows)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == len(rows):
        return cached[1], cached[2], cached[3]
    times: list[int] = []
    ps = [0.0]
    ps2 = [0.0]
    for row in rows:
        times.append(row.ts)
        ps.append(ps[-1] + row.value)
        ps2.append(ps2[-1] + row.value * row.value)
    _CACHE[key] = (len(rows), times, ps, ps2)
    return times, ps, ps2


def cached_at_or_before(rows: list[engine.Sample], ts: int) -> tuple[int, float] | None:
    if not rows:
        return None
    times, _, _ = _meta(rows)
    idx = bisect.bisect_right(times, ts) - 1
    if idx < 0:
        return None
    return idx, rows[idx].value


def cached_funding_features(rows: list[engine.Sample], ts: int) -> tuple[float | None, float | None]:
    hit = cached_at_or_before(rows, ts)
    if hit is None:
        return None, None
    idx, value = hit
    if idx + 1 < protocol.MIN_FUNDING_HISTORY:
        return value, None
    start = max(0, idx + 1 - protocol.FUNDING_Z_LOOKBACK)
    count = idx + 1 - start
    if count < protocol.MIN_FUNDING_HISTORY:
        return value, None
    _, ps, ps2 = _meta(rows)
    total = ps[idx + 1] - ps[start]
    total2 = ps2[idx + 1] - ps2[start]
    mean = total / count
    variance = max(0.0, total2 / count - mean * mean)
    sd = math.sqrt(variance)
    if not math.isfinite(sd) or sd <= 1e-12:
        return value, 0.0
    return value, (value - mean) / sd


def cached_pct_change_series(rows: list[engine.Sample], ts: int, hours: int) -> float | None:
    now_hit = cached_at_or_before(rows, ts)
    old_hit = cached_at_or_before(rows, ts - hours * engine.HOUR_MS)
    if now_hit is None or old_hit is None:
        return None
    now_value = now_hit[1]
    old_value = old_hit[1]
    if old_value <= 0:
        return None
    return now_value / old_value - 1.0


def main() -> int:
    engine.at_or_before = cached_at_or_before
    engine.funding_features = cached_funding_features
    engine.pct_change_series = cached_pct_change_series
    return engine.main()


if __name__ == "__main__":
    raise SystemExit(main())
