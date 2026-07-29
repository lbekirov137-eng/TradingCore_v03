"""
Тесты аудита качества исторического датасета.

Ключевое проверяемое свойство: пропуски НИКОГДА не заполняются. Аудит
обязан их находить и сообщать, а не чинить — интерполированная свеча это
выдуманная цена, и стратегия, проверенная на ней, проверена на том, чего
не было.
"""

import json
from pathlib import Path

import pytest

from api.strategy_engine.dataset_quality import (
    audit_dataset,
    file_sha256,
    load_research_candles,
    quality_verdict,
)

HOUR = 3_600_000


def write_dataset(tmp_path: Path, rows, interval="1h", **extra) -> Path:
    payload = {
        "symbol": "BTCUSDT",
        "interval": interval,
        "timestamps": [r[0] for r in rows],
        "opens": [r[1] for r in rows],
        "highs": [r[2] for r in rows],
        "lows": [r[3] for r in rows],
        "closes": [r[4] for r in rows],
        "volumes": [r[5] for r in rows],
    }
    payload.update(extra)

    path = tmp_path / "ds.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    return path


def clean_rows(count: int, start: int = 0):
    return [
        (start + i * HOUR, 100.0, 101.0, 99.0, 100.5, 10.0)
        for i in range(count)
    ]


class TestCleanDataset:

    def test_clean_dataset_is_usable(self, tmp_path: Path) -> None:
        report = audit_dataset(write_dataset(tmp_path, clean_rows(100)))

        assert report["ok"] is True
        assert report["valid_candles"] == 100
        assert report["expected_candles"] == 100
        assert report["missing_candles"] == 0
        assert report["completeness_percent"] == 100.0
        assert report["gap_count"] == 0
        assert quality_verdict(report)["usable"] is True

    def test_checksum_is_reported(self, tmp_path: Path) -> None:
        path = write_dataset(tmp_path, clean_rows(10))

        assert audit_dataset(path)["file_sha256"] == file_sha256(path)

    def test_provenance_is_preserved(self, tmp_path: Path) -> None:
        path = write_dataset(
            tmp_path, clean_rows(10),
            provenance={"provider": "Binance public REST API",
                        "api_key_used": False},
        )

        report = audit_dataset(path)

        assert report["provenance"]["provider"] == "Binance public REST API"
        assert report["provenance"]["api_key_used"] is False


class TestGapsAreReportedNotFilled:

    def test_gap_is_detected_and_counted(self, tmp_path: Path) -> None:
        rows = clean_rows(50)
        del rows[20:23]          # три пропущенных часа

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["gap_count"] == 1
        assert report["missing_candles"] == 3
        assert report["gaps"][0]["missing_candles"] == 3

    def test_gap_is_never_interpolated(self, tmp_path: Path) -> None:
        """Число валидных свечей не растёт — дыра не «залечена»."""
        rows = clean_rows(50)
        del rows[20:23]

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["valid_candles"] == 47
        assert report["expected_candles"] == 50
        assert report["gap_filling"].startswith("NONE")

    def test_large_gap_fails_completeness(self, tmp_path: Path) -> None:
        rows = clean_rows(100)
        del rows[10:40]

        report = audit_dataset(write_dataset(tmp_path, rows))
        verdict = quality_verdict(report)

        assert verdict["usable"] is False
        assert any("completeness" in r for r in verdict["reasons"])


class TestCorruptionDetection:

    def test_duplicates_are_counted_and_block_use(self, tmp_path: Path) -> None:
        rows = clean_rows(20)
        rows.append(rows[5])

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["duplicate_timestamps"] == 1
        assert quality_verdict(report)["usable"] is False

    def test_out_of_order_rows_are_detected(self, tmp_path: Path) -> None:
        rows = clean_rows(20)
        rows[3], rows[9] = rows[9], rows[3]

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["out_of_order_rows"] >= 1
        assert quality_verdict(report)["usable"] is False

    def test_malformed_ohlc_is_rejected(self, tmp_path: Path) -> None:
        rows = clean_rows(20)
        rows[5] = (rows[5][0], 100.0, 90.0, 110.0, 100.0, 10.0)  # low > high

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["malformed_ohlc"] == 1

    def test_negative_price_is_rejected(self, tmp_path: Path) -> None:
        rows = clean_rows(20)
        rows[5] = (rows[5][0], -1.0, 101.0, -2.0, 100.0, 10.0)

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["negative_or_zero_price"] == 1
        assert quality_verdict(report)["usable"] is False

    def test_zero_volume_is_reported_but_not_fatal(self, tmp_path: Path) -> None:
        """Нулевой объём не портит цену, но VWAP на нём не определён."""
        rows = clean_rows(20)
        rows[5] = (rows[5][0], 100.0, 101.0, 99.0, 100.5, 0.0)

        report = audit_dataset(write_dataset(tmp_path, rows))

        assert report["zero_or_negative_volume"] == 1
        assert report["valid_candles"] == 20

    def test_ragged_columns_are_detected(self, tmp_path: Path) -> None:
        payload = {
            "symbol": "BTCUSDT", "interval": "1h",
            "timestamps": [0, HOUR, 2 * HOUR],
            "opens": [1.0, 1.0], "highs": [2.0, 2.0, 2.0],
            "lows": [0.5, 0.5, 0.5], "closes": [1.5, 1.5, 1.5],
            "volumes": [1.0, 1.0, 1.0],
        }
        path = tmp_path / "ragged.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        report = audit_dataset(path)

        assert report["ragged_columns"] is True
        assert quality_verdict(report)["usable"] is False

    def test_missing_file_is_reported(self, tmp_path: Path) -> None:
        report = audit_dataset(tmp_path / "absent.json")

        assert report["ok"] is False
        assert report["error"] == "FILE_NOT_FOUND"
        assert quality_verdict(report)["usable"] is False


class TestLoading:

    def test_loads_candles_in_chronological_order(self, tmp_path: Path) -> None:
        rows = clean_rows(30)
        rows[2], rows[7] = rows[7], rows[2]

        candles = load_research_candles(write_dataset(tmp_path, rows))

        stamps = [c.open_time_ms for c in candles]
        assert stamps == sorted(stamps)

    def test_malformed_rows_are_dropped_not_repaired(self, tmp_path: Path) -> None:
        rows = clean_rows(20)
        rows[5] = (rows[5][0], 100.0, 90.0, 110.0, 100.0, 10.0)

        candles = load_research_candles(write_dataset(tmp_path, rows))

        assert len(candles) == 19


class TestRealResearchDataset:
    """
    Проверки на фактически загруженном файле, если он присутствует.
    Пропускаются, а не падают: файл не коммитится и может отсутствовать
    в чистом checkout.
    """

    PATH = Path("data/research/BTCUSDT_1h_2y.json")

    def test_real_dataset_quality(self) -> None:
        if not self.PATH.exists():
            pytest.skip("research dataset not present in this checkout")

        report = audit_dataset(self.PATH)
        verdict = quality_verdict(report)

        assert report["ok"] is True
        assert verdict["usable"] is True, verdict["reasons"]
        assert report["duplicate_timestamps"] == 0
        assert report["out_of_order_rows"] == 0
        assert report["coverage_years"] >= 2.0

    def test_real_dataset_records_no_api_key(self) -> None:
        if not self.PATH.exists():
            pytest.skip("research dataset not present in this checkout")

        provenance = audit_dataset(self.PATH)["provenance"]

        assert provenance.get("api_key_used") is False
        assert provenance.get("provider")
        assert provenance.get("downloaded_at_utc")
