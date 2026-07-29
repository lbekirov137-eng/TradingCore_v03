"""
Где живут данные и переживут ли они рестарт.

ПРОБЛЕМА. В контейнере `data/` попадает под .dockerignore, а файловая
система эфемерна: при каждом redeploy журнал исчезает. Пока это не видно
снаружи, отчёт после рестарта показывает ноль сделок и выглядит так,
будто монитор не работал, — вместо того чтобы сказать «история потеряна».

Поэтому персистентность здесь не предполагается, а ПРОВЕРЯЕТСЯ, и её
отсутствие — явный факт в /performance/data-health, а не тишина.

Railway монтирует volume и выставляет RAILWAY_VOLUME_MOUNT_PATH. Хранилище
считается долговечным, только если корень данных лежит ВНУТРИ смонтированного
volume. Совпадение имён или наличие переменной самой по себе недостаточно:
переменная может быть выставлена, а PAPER_DATA_DIR указывать мимо.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# Имя, под которым Railway сообщает точку монтирования volume.
RAILWAY_VOLUME_ENV = "RAILWAY_VOLUME_MOUNT_PATH"

# Переменная, которой задаётся корень данных (уже используется циклом).
DATA_DIR_ENV = "PAPER_DATA_DIR"

DEFAULT_DATA_DIR = "data"

JOURNAL_NAME = "paper_runs.jsonl"
CLOSED_TRADES_NAME = "closed_trades.jsonl"
DAILY_REPORTS_NAME = "daily_reports.jsonl"
STRATEGY_REPORTS_NAME = "strategy_reports.jsonl"
SUPERVISOR_STATE_NAME = "supervisor_state.json"
ARCHIVE_DIR_NAME = "archive"


def data_root() -> Path:
    return Path(os.getenv(DATA_DIR_ENV, DEFAULT_DATA_DIR))


def volume_mount_path() -> Path | None:
    raw = os.getenv(RAILWAY_VOLUME_ENV)

    if not raw or not raw.strip():
        return None

    return Path(raw.strip())


def _is_within(child: Path, parent: Path) -> bool:
    """
    Лежит ли child внутри parent.

    Сравнение по разрешённым абсолютным путям: относительный путь и
    символическая ссылка иначе дали бы неверный ответ в обе стороны.
    """
    try:
        child_resolved = child.resolve()
        parent_resolved = parent.resolve()
    except OSError:
        return False

    if child_resolved == parent_resolved:
        return True

    return parent_resolved in child_resolved.parents


def storage_is_persistent() -> bool:
    """
    Долговечно ли хранилище.

    Строго: volume должен быть смонтирован И корень данных должен лежать
    внутри него. Ответ «не знаю» здесь недопустим — он означал бы, что мы
    не можем сказать, потеряются ли данные, а это худший из ответов.
    """
    mount = volume_mount_path()

    if mount is None:
        return False

    return _is_within(data_root(), mount)


def storage_diagnosis() -> dict[str, Any]:
    """
    Почему хранилище (не)долговечно — в терминах, по которым это чинится.

    Возвращает не только флаг, но и конкретное действие: без него
    «persistent: false» не подсказывает, что именно настроить.
    """
    mount = volume_mount_path()
    root = data_root()
    persistent = storage_is_persistent()

    if persistent:
        detail = (
            f"data root {root} is inside the mounted volume {mount}"
        )
        action = None
    elif mount is None:
        detail = (
            f"{RAILWAY_VOLUME_ENV} is not set: no volume is mounted, so the "
            "container filesystem is ephemeral and the journal is lost on "
            "every redeploy"
        )
        action = (
            "Attach a Railway volume to this service, then set "
            f"{DATA_DIR_ENV} to a path inside "
            f"{RAILWAY_VOLUME_ENV}"
        )
    else:
        detail = (
            f"a volume is mounted at {mount}, but the data root {root} is "
            "outside it, so records still land on the ephemeral filesystem"
        )
        action = f"Set {DATA_DIR_ENV} to a path inside {mount}"

    return {
        "persistent": persistent,
        "data_root": str(root),
        "volume_mount_path": str(mount) if mount else None,
        "detail": detail,
        "required_action": action,
    }


def journal_file() -> Path:
    return data_root() / JOURNAL_NAME


def closed_trades_file() -> Path:
    return data_root() / CLOSED_TRADES_NAME


def daily_reports_file() -> Path:
    return data_root() / DAILY_REPORTS_NAME


def strategy_reports_file() -> Path:
    return data_root() / STRATEGY_REPORTS_NAME


def supervisor_state_file() -> Path:
    return data_root() / SUPERVISOR_STATE_NAME


def archive_dir() -> Path:
    return data_root() / ARCHIVE_DIR_NAME
