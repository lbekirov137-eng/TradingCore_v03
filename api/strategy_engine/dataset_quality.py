"""
Аудит качества исторического датасета.

ЗАЧЕМ ОТДЕЛЬНО ОТ ЗАГРУЗКИ. Загрузчик — одноразовый скрипт, а проверка
качества обязана выполняться при КАЖДОМ использовании файла: датасет может
быть заменён, обрезан или частично записан, и валидация, запущенная на
испорченных данных, выдаст уверенные и неверные числа.

Принципиальное правило: пропуски НИКОГДА не заполняются. Интерполированная
свеча — это выдуманная цена, а стратегия, обученная на выдуманных ценах,
проверена на том, чего не было. Пропуски только фиксируются и оцениваются.

Модуль ничего не изменяет: он открывает файл на чтение и возвращает отчёт.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.strategy_engine.strategies.contracts import Candle


INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "1H": 3_600_000,
    "4h": 14_400_000,
    "4H": 14_400_000,
}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()

    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def audit_dataset(path: str | Path) -> dict[str, Any]:
    """
    Полный отчёт о качестве. Не бросает исключение на плохих данных —
    плохие данные это ВЫВОД аудита, а не повод ему упасть.
    """
    path = Path(path)

    if not path.exists():
        return {"ok": False, "error": "FILE_NOT_FOUND", "path": str(path)}

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    interval = payload.get("interval", "1h")
    step = INTERVAL_MS.get(interval)

    if step is None:
        return {"ok": False, "error": "UNKNOWN_INTERVAL", "interval": interval}

    timestamps = payload.get("timestamps") or []
    opens = payload.get("opens") or []
    highs = payload.get("highs") or []
    lows = payload.get("lows") or []
    closes = payload.get("closes") or []
    volumes = payload.get("volumes") or []

    lengths = {
        "timestamps": len(timestamps), "opens": len(opens),
        "highs": len(highs), "lows": len(lows),
        "closes": len(closes), "volumes": len(volumes),
    }
    ragged = len(set(lengths.values())) != 1

    size = min(lengths.values()) if lengths else 0

    seen: set[int] = set()
    duplicates = 0
    out_of_order = 0
    malformed = 0
    negative_price = 0
    non_positive_volume = 0

    previous = None
    valid: list[tuple[int, float, float, float, float, float]] = []

    for index in range(size):
        stamp = timestamps[index]

        if not isinstance(stamp, (int, float)):
            malformed += 1
            continue

        stamp = int(stamp)

        if previous is not None and stamp < previous:
            out_of_order += 1

        previous = stamp

        if stamp in seen:
            duplicates += 1
            continue

        seen.add(stamp)

        try:
            o, h, l, c, v = (
                float(opens[index]), float(highs[index]), float(lows[index]),
                float(closes[index]), float(volumes[index]),
            )
        except (TypeError, ValueError):
            malformed += 1
            continue

        if any(not math.isfinite(x) for x in (o, h, l, c, v)):
            malformed += 1
            continue

        if min(o, h, l, c) <= 0:
            negative_price += 1
            continue

        if l > h or not (l <= o <= h) or not (l <= c <= h):
            malformed += 1
            continue

        if v <= 0:
            # Нулевой объём не делает свечу невалидной по цене, но должен
            # быть виден: VWAP на таком участке не определён.
            non_positive_volume += 1

        valid.append((stamp, o, h, l, c, v))

    valid.sort(key=lambda row: row[0])

    gaps: list[dict[str, Any]] = []

    for index in range(1, len(valid)):
        delta = valid[index][0] - valid[index - 1][0]

        if delta != step:
            missing = delta // step - 1

            gaps.append({
                "after_utc": _utc(valid[index - 1][0]),
                "before_utc": _utc(valid[index][0]),
                "missing_candles": int(missing),
            })

    first_ms = valid[0][0] if valid else None
    last_ms = valid[-1][0] if valid else None

    expected = (
        int((last_ms - first_ms) // step) + 1
        if first_ms is not None and last_ms is not None
        else 0
    )

    missing_total = sum(item["missing_candles"] for item in gaps)

    coverage_days = (
        (last_ms - first_ms) / 86_400_000 if first_ms is not None else 0.0
    )

    return {
        "ok": True,
        "path": str(path),
        "file_sha256": file_sha256(path),
        "provenance": payload.get("provenance", {}),
        "interval": interval,
        "step_ms": step,
        "column_lengths": lengths,
        "ragged_columns": ragged,
        "total_rows": size,
        "valid_candles": len(valid),
        "expected_candles": expected,
        "missing_candles": missing_total,
        "completeness_percent": (
            round(len(valid) / expected * 100.0, 4) if expected else None
        ),
        "duplicate_timestamps": duplicates,
        "out_of_order_rows": out_of_order,
        "malformed_ohlc": malformed,
        "negative_or_zero_price": negative_price,
        "zero_or_negative_volume": non_positive_volume,
        "gap_count": len(gaps),
        "gaps": gaps[:20],
        "first_utc": _utc(first_ms) if first_ms is not None else None,
        "last_utc": _utc(last_ms) if last_ms is not None else None,
        "coverage_days": round(coverage_days, 2),
        "coverage_years": round(coverage_days / 365.0, 3),
        "gap_filling": "NONE - gaps reported, never interpolated",
    }


def load_research_candles(path: str | Path) -> list[Candle]:
    """
    Загружает свечи research-датасета.

    Некорректные строки ОТБРАСЫВАЮТСЯ, а не чинятся: молча исправленная
    свеча — это выдуманные данные. Порядок принудительно хронологический.
    """
    from api.strategy_engine.strategies.contracts import candles_from_arrays

    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    return candles_from_arrays(payload)


def quality_verdict(
    report: dict[str, Any],
    min_completeness: float = 99.0,
    max_duplicates: int = 0,
    max_out_of_order: int = 0,
) -> dict[str, Any]:
    """
    Пригоден ли датасет для валидации.

    Пороги строгие намеренно: валидация на дырявых данных даёт уверенные
    числа, которые нельзя проверить, и это хуже отсутствия чисел.
    """
    if not report.get("ok"):
        return {"usable": False, "reasons": [report.get("error", "UNKNOWN")]}

    reasons: list[str] = []

    completeness = report.get("completeness_percent")

    if completeness is None or completeness < min_completeness:
        reasons.append(
            f"completeness {completeness}% below required {min_completeness}%"
        )

    if report["duplicate_timestamps"] > max_duplicates:
        reasons.append(f"{report['duplicate_timestamps']} duplicate timestamps")

    if report["out_of_order_rows"] > max_out_of_order:
        reasons.append(f"{report['out_of_order_rows']} out-of-order rows")

    if report["ragged_columns"]:
        reasons.append("OHLCV columns have different lengths")

    if report["negative_or_zero_price"]:
        reasons.append(
            f"{report['negative_or_zero_price']} candles with non-positive price"
        )

    return {"usable": not reasons, "reasons": reasons}
