"""
Долговечное хранилище paper-контура.

Отвечает за то, чтобы накопленная статистика не зависела от эфемерной
файловой системы контейнера: атомарная запись, дедупликация по
observation_id и trade_id, восстановление после рестарта, retention с
архивированием и честный отчёт о здоровье данных.

Ничего не решает и не торгует — только пишет и читает.
"""

from api.paper_storage.health import (
    RETENTION_DAYS,
    apply_retention,
    data_health,
    read_restart_marker,
    record_startup,
)
from api.paper_storage.paths import (
    archive_dir,
    closed_trades_file,
    daily_reports_file,
    data_root,
    journal_file,
    storage_diagnosis,
    storage_is_persistent,
    strategy_reports_file,
    supervisor_state_file,
    volume_mount_path,
)
from api.paper_storage.store import (
    JsonlStore,
    StorageError,
    observation_id,
    read_json,
    trade_id,
    write_json_atomic,
)

__all__ = [
    "JsonlStore",
    "StorageError",
    "RETENTION_DAYS",
    "apply_retention",
    "data_health",
    "record_startup",
    "read_restart_marker",
    "observation_id",
    "trade_id",
    "read_json",
    "write_json_atomic",
    "archive_dir",
    "closed_trades_file",
    "daily_reports_file",
    "data_root",
    "journal_file",
    "storage_diagnosis",
    "storage_is_persistent",
    "strategy_reports_file",
    "supervisor_state_file",
    "volume_mount_path",
]
