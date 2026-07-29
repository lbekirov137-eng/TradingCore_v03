"""
Тесты долговечного хранилища: атомарность, дедупликация, восстановление.

Центральный тест — имитация рестарта (TestRestartRecovery). Он проверяет
то, ради чего хранилище и существует: после перезапуска процесса история
не теряется и не удваивается.
"""

import json
from pathlib import Path

import pytest

from api.paper_storage import (
    JsonlStore,
    apply_retention,
    data_health,
    observation_id,
    read_json,
    record_startup,
    storage_diagnosis,
    storage_is_persistent,
    trade_id,
    write_json_atomic,
)


def cycle(close_time_ms: int, utc: str, net_pnl: float | None = None) -> dict:
    record = {
        "recorded_at_utc": utc,
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "last_close_time_ms": close_time_ms,
        "real_order_sent": False,
        "position_event": {"event": "POSITION_REMAINS_OPEN"},
    }

    if net_pnl is not None:
        record["position_event"] = {
            "event": "POSITION_CLOSED",
            "net_pnl": net_pnl,
            "position": {
                "opened_at_utc": f"opened_{close_time_ms}",
                "entry": 100.0,
                "stop": 90.0,
                "net_pnl": net_pnl,
            },
        }

    return record


class TestIdentity:

    def test_observation_id_is_deterministic(self) -> None:
        """Тот же id после перезапуска процесса — иначе дедуп не работает."""
        first = observation_id(cycle(1000, "2026-07-01T10:00:00+00:00"))
        second = observation_id(cycle(1000, "2026-07-01T10:00:00+00:00"))

        assert first == second

    def test_reprocessed_candle_keeps_its_id(self) -> None:
        """
        Ключевое свойство: после рестарта цикл переобрабатывает последнюю
        свечу, и время ЗАПИСИ будет другим. Идентичность обязана
        определяться свечой, а не моментом записи.
        """
        original = cycle(1000, "2026-07-01T10:00:00+00:00")
        reprocessed = cycle(1000, "2026-07-01T10:07:33+00:00")

        assert observation_id(original) == observation_id(reprocessed)

    def test_different_candles_get_different_ids(self) -> None:
        assert observation_id(cycle(1000, "a")) != observation_id(
            cycle(2000, "a")
        )

    def test_trade_id_only_for_closed_positions(self) -> None:
        assert trade_id(cycle(1000, "utc")) is None
        assert trade_id(cycle(1000, "utc", net_pnl=2.0)) is not None

    def test_trade_id_survives_reprocessing(self) -> None:
        first = trade_id(cycle(1000, "2026-07-01T10:00:00+00:00", 2.0))
        second = trade_id(cycle(1000, "2026-07-01T10:09:00+00:00", 2.0))

        assert first == second


class TestAtomicWrites:

    def test_append_writes_complete_lines(self, tmp_path: Path) -> None:
        store = JsonlStore(tmp_path / "journal.jsonl")

        for index in range(5):
            store.append(cycle(1000 + index, f"2026-07-01T10:0{index}:00+00:00"))

        lines = (tmp_path / "journal.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()

        assert len(lines) == 5

        for line in lines:
            parsed = json.loads(line)
            assert "observation_id" in parsed
            assert "stored_at_utc" in parsed

    def test_json_state_is_written_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"

        write_json_atomic(target, {"champion": "RANGE_NO_TRADE_POLICY"})

        assert read_json(target)["champion"] == "RANGE_NO_TRADE_POLICY"
        # Временный файл не остаётся рядом.
        assert list(tmp_path.glob("*.tmp")) == []

    def test_corrupted_state_falls_back_instead_of_crashing(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "state.json"
        target.write_text("{ truncated", encoding="utf-8")

        assert read_json(target, default={"safe": True}) == {"safe": True}


class TestDeduplication:

    def test_same_record_is_written_once(self, tmp_path: Path) -> None:
        store = JsonlStore(tmp_path / "journal.jsonl")

        first = store.append(cycle(1000, "2026-07-01T10:00:00+00:00"))
        second = store.append(cycle(1000, "2026-07-01T10:00:00+00:00"))

        assert first["status"] == "WRITTEN"
        assert second["status"] == "DUPLICATE"
        assert second["written"] is False
        assert store.count() == 1

    def test_duplicate_is_reported_not_silently_dropped(
        self, tmp_path: Path
    ) -> None:
        store = JsonlStore(tmp_path / "journal.jsonl")

        store.append(cycle(1000, "utc"))
        store.append(cycle(1000, "utc"))

        assert store.duplicates_skipped == 1


class TestRestartRecovery:
    """
    Имитация рестарта: новый JsonlStore на том же файле — это в точности
    то, что происходит при перезапуске процесса.
    """

    def test_history_survives_a_restart(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.jsonl"

        before = JsonlStore(journal)

        for index in range(10):
            before.append(
                cycle(1000 + index, f"2026-07-01T10:{index:02d}:00+00:00")
            )

        assert before.count() == 10

        # --- процесс перезапущен ---
        after = JsonlStore(journal)

        assert after.count() == 10, "history must survive the restart"

    def test_reprocessed_candle_is_not_written_twice_after_restart(
        self, tmp_path: Path
    ) -> None:
        """
        Самый важный тест хранилища. После рестарта цикл переобрабатывает
        последнюю свечу; без дедупликации она удвоила бы сделку в
        статистике.
        """
        journal = tmp_path / "journal.jsonl"

        before = JsonlStore(journal)
        before.append(cycle(1000, "2026-07-01T10:00:00+00:00", net_pnl=2.0))
        before.append(cycle(2000, "2026-07-01T10:05:00+00:00", net_pnl=3.0))

        # --- рестарт: индекс строится заново из файла ---
        after = JsonlStore(journal)

        replay = after.append(
            cycle(2000, "2026-07-01T10:11:47+00:00", net_pnl=3.0)
        )

        assert replay["status"] == "DUPLICATE"
        assert after.count() == 2

        health = data_health(journal)

        assert health["closed_trades_count"] == 2
        assert health["duplicate_count"] == 0

    def test_startup_marker_reports_restoration(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.jsonl"

        store = JsonlStore(journal)
        store.append(cycle(1000, "2026-07-01T10:00:00+00:00"))

        marker = record_startup(journal)

        assert marker["restored"] is True
        assert marker["records_found_at_startup"] == 1

    def test_startup_marker_on_empty_history(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("PAPER_DATA_DIR", str(tmp_path))

        marker = record_startup(tmp_path / "absent.jsonl")

        assert marker["restored"] is False
        assert marker["records_found_at_startup"] == 0

    def test_corrupted_tail_does_not_lose_earlier_history(
        self, tmp_path: Path
    ) -> None:
        """
        SIGKILL посреди записи оставляет оборванную последнюю строку.
        Ранее записанное обязано остаться читаемым.
        """
        journal = tmp_path / "journal.jsonl"

        store = JsonlStore(journal)
        store.append(cycle(1000, "2026-07-01T10:00:00+00:00", net_pnl=1.0))
        store.append(cycle(2000, "2026-07-01T10:05:00+00:00", net_pnl=1.0))

        with journal.open("a", encoding="utf-8") as file:
            file.write('{"recorded_at_utc": "2026-07-01T10:10')

        health = data_health(journal)

        assert health["record_count"] == 2
        assert health["corrupted_records"] == 1
        assert health["closed_trades_count"] == 2


class TestPersistenceDetection:

    def test_no_volume_means_not_persistent(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
        monkeypatch.setenv("PAPER_DATA_DIR", str(tmp_path))

        assert storage_is_persistent() is False

        diagnosis = storage_diagnosis()

        assert "no volume is mounted" in diagnosis["detail"]
        assert diagnosis["required_action"]

    def test_data_root_outside_volume_is_not_persistent(
        self, monkeypatch, tmp_path
    ) -> None:
        """
        Тонкий случай: volume подключён, но PAPER_DATA_DIR указывает мимо.
        Наличие переменной само по себе ничего не гарантирует.
        """
        volume = tmp_path / "volume"
        outside = tmp_path / "elsewhere"
        volume.mkdir()
        outside.mkdir()

        monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(volume))
        monkeypatch.setenv("PAPER_DATA_DIR", str(outside))

        assert storage_is_persistent() is False
        assert "outside it" in storage_diagnosis()["detail"]

    def test_data_root_inside_volume_is_persistent(
        self, monkeypatch, tmp_path
    ) -> None:
        volume = tmp_path / "volume"
        inside = volume / "paper"
        inside.mkdir(parents=True)

        monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(volume))
        monkeypatch.setenv("PAPER_DATA_DIR", str(inside))

        assert storage_is_persistent() is True
        assert storage_diagnosis()["required_action"] is None


class TestDataHealth:

    def test_empty_journal_is_explained_not_just_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
        monkeypatch.setenv("PAPER_DATA_DIR", str(tmp_path))

        health = data_health(tmp_path / "absent.jsonl")

        assert health["record_count"] == 0
        assert health["storage_persistent"] is False
        # Пустота обязана быть объяснена, а не выглядеть как «сделок нет».
        assert any("journal is empty" in w for w in health["warnings"])
        assert any("STORAGE_NOT_PERSISTENT" in w for w in health["warnings"])

    def test_health_reports_boundaries_and_counts(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.jsonl"
        store = JsonlStore(journal)

        store.append(cycle(1000, "2026-07-01T10:00:00+00:00", net_pnl=1.0))
        store.append(cycle(2000, "2026-07-02T10:00:00+00:00"))
        store.append(cycle(3000, "2026-07-03T10:00:00+00:00", net_pnl=-1.0))

        health = data_health(journal)

        assert health["record_count"] == 3
        assert health["first_record_utc"] == "2026-07-01T10:00:00+00:00"
        assert health["last_record_utc"] == "2026-07-03T10:00:00+00:00"
        assert health["closed_trades_count"] == 2
        assert health["duplicate_count"] == 0

    def test_a_string_path_is_accepted(self, tmp_path: Path) -> None:
        """
        Регрессия: data_health вызывал path.exists() на str и падал с
        AttributeError. Тесты передавали Path и не ловили это; поймала
        живая демонстрация.
        """
        journal = tmp_path / "journal.jsonl"
        JsonlStore(journal).append(cycle(1000, "2026-07-01T10:00:00+00:00"))

        health = data_health(str(journal))

        assert health["record_count"] == 1
        assert health["journal_exists"] is True

    def test_retention_and_startup_accept_string_paths(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.jsonl"
        JsonlStore(journal).append(cycle(1000, "2026-07-28T10:00:00+00:00"))

        assert record_startup(str(journal))["restored"] is True
        assert apply_retention(
            str(journal), retention_days=90, now_utc="2026-07-29T00:00:00+00:00"
        )["status"] == "NOTHING_TO_ARCHIVE"

    def test_pre_existing_duplicates_are_counted(self, tmp_path: Path) -> None:
        """
        Дубликаты, записанные ПРОШЛЫМ запуском, обязаны быть видны:
        счётчик текущего процесса о них ничего не знает.
        """
        journal = tmp_path / "journal.jsonl"

        record = cycle(1000, "2026-07-01T10:00:00+00:00")
        line = json.dumps(record) + "\n"

        journal.write_text(line + line, encoding="utf-8")

        assert data_health(journal)["duplicate_count"] == 1


class TestRetention:

    def test_old_records_are_archived_not_deleted(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.jsonl"
        store = JsonlStore(journal)

        store.append(cycle(1000, "2026-01-01T10:00:00+00:00", net_pnl=1.0))
        store.append(cycle(2000, "2026-07-28T10:00:00+00:00", net_pnl=1.0))

        result = apply_retention(
            journal,
            retention_days=90,
            now_utc="2026-07-29T00:00:00+00:00",
        )

        assert result["status"] == "ARCHIVED"
        assert result["archived"] == 1
        assert result["kept"] == 1

        # История НЕ потеряна — она в архиве.
        archived = Path(result["archive_file"]).read_text(encoding="utf-8")
        assert "2026-01-01T10:00:00+00:00" in archived

        remaining = JsonlStore(journal).read_all()
        assert len(remaining) == 1

    def test_records_without_a_timestamp_are_never_archived(
        self, tmp_path: Path
    ) -> None:
        """Архивировать по незнанию — это потеря."""
        journal = tmp_path / "journal.jsonl"

        journal.write_text(
            json.dumps({"symbol": "BTCUSDT", "no_timestamp": True}) + "\n",
            encoding="utf-8",
        )

        result = apply_retention(
            journal, retention_days=1, now_utc="2026-07-29T00:00:00+00:00"
        )

        assert result["archived"] == 0
        assert result["kept"] == 1

    def test_nothing_to_archive_is_reported(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.jsonl"
        JsonlStore(journal).append(cycle(1000, "2026-07-28T10:00:00+00:00"))

        result = apply_retention(
            journal, retention_days=90, now_utc="2026-07-29T00:00:00+00:00"
        )

        assert result["status"] == "NOTHING_TO_ARCHIVE"
