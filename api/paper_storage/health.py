"""
Здоровье хранилища и retention.

ЗАЧЕМ ОТДЕЛЬНЫЙ ОТЧЁТ. Пустой отчёт о результатах выглядит одинаково в
двух совершенно разных случаях: «монитор работал, сделок не было» и
«история потеряна при redeploy, потому что volume не подключён». Первый —
нормальная работа, второй — потеря данных. /performance/data-health
существует, чтобы их нельзя было перепутать.

Retention: журнал растёт линейно по свечам (при 5m это ~288 записей в
сутки на символ). Старые записи не удаляются, а ПЕРЕНОСЯТСЯ в архив:
удаление истории ради места обесценило бы всю накопленную статистику,
ради которой контур и работает.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from api.paper_storage import paths
from api.paper_storage.store import (
    JsonlStore,
    observation_id,
    trade_id,
    utc_now,
    write_json_atomic,
)


# Сколько суток журнала держим «горячими». 90 дней при 5m — около 26k
# записей на символ: это ещё быстро читается целиком и покрывает
# требование «несколько торговых дней и несколько режимов» с запасом.
RETENTION_DAYS = 90


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


def data_health(
    journal_file: Path | None = None,
    restart_marker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Полный отчёт о состоянии хранилища.

    Считает дубликаты ФАКТИЧЕСКИ — пересчётом id по всему файлу, а не по
    счётчику текущего процесса: дубликаты, записанные прошлым запуском,
    иначе остались бы невидимыми.
    """
    # Приведение к Path обязательно: вызывающий вправе передать строку,
    # и path.exists() ниже иначе падает с AttributeError.
    path = Path(journal_file) if journal_file is not None else paths.journal_file()

    store = JsonlStore(path)

    records = store.read_all()
    corrupted = store.corrupted_count

    seen: set[str] = set()
    duplicates = 0
    closed_trades: set[str] = set()
    duplicate_trades = 0

    first_utc: str | None = None
    last_utc: str | None = None

    for record in records:
        identifier = observation_id(record)

        if identifier in seen:
            duplicates += 1
        else:
            seen.add(identifier)

        related_trade = trade_id(record)

        if related_trade is not None:
            if related_trade in closed_trades:
                duplicate_trades += 1
            else:
                closed_trades.add(related_trade)

        stamp = record.get("recorded_at_utc")

        if isinstance(stamp, str) and stamp:
            if first_utc is None or stamp < first_utc:
                first_utc = stamp

            if last_utc is None or stamp > last_utc:
                last_utc = stamp

    diagnosis = paths.storage_diagnosis()

    marker = restart_marker if restart_marker is not None else read_restart_marker()

    return {
        "schema_version": "PAPER_DATA_HEALTH_V1",
        "mode": "PAPER",
        "storage_persistent": diagnosis["persistent"],
        "storage": diagnosis,
        "journal_file": str(path),
        "journal_exists": path.exists(),
        "first_record_utc": first_utc,
        "last_record_utc": last_utc,
        "record_count": len(records),
        "unique_record_count": len(seen),
        "closed_trades_count": len(closed_trades),
        "duplicate_count": duplicates,
        "duplicate_trade_count": duplicate_trades,
        "corrupted_records": corrupted,
        "restored_after_restart": bool(marker and marker.get("restored")),
        "restart_marker": marker,
        "retention_days": RETENTION_DAYS,
        "archive_dir": str(paths.archive_dir()),
        "warnings": _warnings(
            diagnosis, duplicates, corrupted, len(records)
        ),
    }


def _warnings(
    diagnosis: dict[str, Any],
    duplicates: int,
    corrupted: int,
    total: int,
) -> list[str]:
    warnings: list[str] = []

    if not diagnosis["persistent"]:
        warnings.append(
            "STORAGE_NOT_PERSISTENT: " + diagnosis["detail"]
        )

    if duplicates:
        warnings.append(
            f"{duplicates} duplicate record(s) present in the journal"
        )

    if corrupted:
        warnings.append(
            f"{corrupted} corrupted line(s) skipped while reading"
        )

    if total == 0:
        warnings.append(
            "journal is empty: either the monitor has not run yet, or "
            "history was lost. Check storage_persistent above before "
            "reading this as 'no trades'."
        )

    return warnings


# ------------------------------------------------------------ restart marker


def read_restart_marker() -> dict[str, Any] | None:
    from api.paper_storage.store import read_json

    return read_json(paths.data_root() / "restart_marker.json", default=None)


def record_startup(
    journal_file: Path | None = None,
) -> dict[str, Any]:
    """
    Отмечает старт процесса и фиксирует, восстановлена ли история.

    Вызывается один раз при запуске цикла. Именно эта отметка отвечает на
    вопрос «данные пережили рестарт или начались заново» — без неё пустой
    журнал после redeploy выглядел бы как первый запуск.
    """
    path = Path(journal_file) if journal_file is not None else paths.journal_file()

    store = JsonlStore(path)
    existing = store.count()

    marker = {
        "started_at_utc": utc_now(),
        "records_found_at_startup": existing,
        "restored": existing > 0,
        "storage_persistent": paths.storage_is_persistent(),
    }

    write_json_atomic(paths.data_root() / "restart_marker.json", marker)

    return marker


# ------------------------------------------------------------ retention


def apply_retention(
    journal_file: Path | None = None,
    retention_days: int = RETENTION_DAYS,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """
    Переносит записи старше retention в архив.

    Архивирование, а НЕ удаление: история — это и есть продукт paper-
    контура. Операция построена так, что потеря невозможна: сначала
    целиком пишется архив, затем целиком пишется усечённый журнал, и
    только через os.replace. Обрыв на любом шаге оставляет исходный файл
    нетронутым.
    """
    path = Path(journal_file) if journal_file is not None else paths.journal_file()

    if not path.exists():
        return {
            "status": "NOTHING_TO_DO",
            "archived": 0,
            "kept": 0,
        }

    now = _parse_utc(now_utc) or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    store = JsonlStore(path)
    records = store.read_all()

    keep: list[dict[str, Any]] = []
    archive: list[dict[str, Any]] = []

    for record in records:
        stamp = _parse_utc(record.get("recorded_at_utc"))

        # Запись без времени НИКОГДА не архивируется: мы не знаем, старая
        # она или нет, а архивировать по незнанию — это потеря.
        if stamp is None or stamp >= cutoff:
            keep.append(record)
        else:
            archive.append(record)

    if not archive:
        return {
            "status": "NOTHING_TO_ARCHIVE",
            "archived": 0,
            "kept": len(keep),
            "cutoff_utc": cutoff.isoformat(),
        }

    archive_path = (
        paths.archive_dir()
        / f"{path.stem}_{cutoff.date().isoformat()}{path.suffix}"
    )

    archive_path.parent.mkdir(parents=True, exist_ok=True)

    import json
    import os
    import tempfile

    # 1) архив пишется первым и целиком
    with archive_path.open("a", encoding="utf-8") as file:
        for record in archive:
            file.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )
        file.flush()
        os.fsync(file.fileno())

    # 2) усечённый журнал через временный файл и os.replace
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )

    with os.fdopen(handle, "w", encoding="utf-8") as file:
        for record in keep:
            file.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary, path)

    return {
        "status": "ARCHIVED",
        "archived": len(archive),
        "kept": len(keep),
        "archive_file": str(archive_path),
        "cutoff_utc": cutoff.isoformat(),
    }
