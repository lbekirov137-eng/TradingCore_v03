"""
Долговечное хранилище наблюдений, сделок и отчётов.

ТРИ СВОЙСТВА, РАДИ КОТОРЫХ ОН СУЩЕСТВУЕТ.

1. АТОМАРНОСТЬ. Контейнер получает SIGKILL посреди записи — это штатное
   событие при redeploy, а не редкость. Строка пишется целиком одним
   вызовом write() вместе с завершающим \\n и сбрасывается на диск через
   fsync, поэтому оборванная запись не может оказаться в файле частично.
   Состояние (JSON) пишется во временный файл и переставляется через
   os.replace — атомарную операцию на обеих платформах.

2. ДЕДУПЛИКАЦИЯ. После рестарта цикл переобрабатывает последнюю свечу:
   так и задумано, иначе свеча терялась бы. Но записывать её второй раз
   нельзя — она удвоила бы сделку в статистике. Идентификатор
   ДЕТЕРМИНИРОВАННЫЙ: он выводится из содержания записи, а не из счётчика,
   поэтому повтор той же свечи даёт тот же id и после перезапуска
   процесса, когда никакого счётчика уже нет.

3. ВОССТАНОВЛЕНИЕ. Индекс уже виденных id строится из файла при старте.
   Битые строки пропускаются и СЧИТАЮТСЯ: «файл частично нечитаем» обязано
   отличаться от «сделок не было».

Хранилище ничего не решает и не торгует. Оно только пишет и читает.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Ключи, по которым строится идентичность наблюдения. Порядок фиксирован:
# от него зависит хеш, поэтому менять его — значит обесценить все ранее
# записанные id.
_OBSERVATION_IDENTITY = (
    "symbol",
    "timeframe",
    "last_close_time_ms",
    "recorded_at_utc",
)


class StorageError(RuntimeError):
    """Отказ хранилища. Осознанно не подавляется тихо."""


def _stable_id(prefix: str, parts: Iterable[Any]) -> str:
    """
    Детерминированный идентификатор из содержания.

    sha1 здесь не криптография, а способ получить короткий стабильный
    ключ: коллизия двух РАЗНЫХ свечей одного символа практически
    невозможна, а совпадение для ОДНОЙ И ТОЙ ЖЕ свечи — именно то, что
    нужно для дедупликации.
    """
    payload = "|".join("" if part is None else str(part) for part in parts)

    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    return f"{prefix}_{digest}"


def observation_id(record: dict[str, Any]) -> str:
    """
    Идентификатор цикла.

    Опирается на время закрытия свечи, а не на время записи: при
    переобработке той же свечи после рестарта recorded_at_utc будет
    другим, а close_time_ms — тем же, и запись будет корректно распознана
    как повтор.
    """
    if not isinstance(record, dict):
        return _stable_id("obs", ("invalid",))

    existing = record.get("observation_id")

    if isinstance(existing, str) and existing:
        return existing

    close_time = record.get("last_close_time_ms")

    parts = [record.get(key) for key in _OBSERVATION_IDENTITY[:3]]

    # Если времени свечи нет (например, запись об ошибке), падаем на
    # время записи — иначе все такие записи схлопнулись бы в один id.
    if close_time is None:
        parts.append(record.get("recorded_at_utc"))
        parts.append(record.get("status"))
        parts.append(record.get("error"))

    return _stable_id("obs", parts)


def trade_id(record: dict[str, Any]) -> str | None:
    """
    Идентификатор ЗАКРЫТОЙ сделки, или None если запись не о закрытии.

    Строится из момента открытия и уровней, а не из момента закрытия:
    одна и та же сделка, переобработанная после рестарта, закроется с
    другим временем записи, но откроется с тем же.
    """
    if not isinstance(record, dict):
        return None

    position_event = record.get("position_event")

    if not isinstance(position_event, dict):
        return None

    if position_event.get("event") != "POSITION_CLOSED":
        return None

    position = position_event.get("position")

    if not isinstance(position, dict):
        position = {}

    return _stable_id(
        "trade",
        (
            record.get("symbol") or position.get("symbol"),
            position.get("opened_at_utc"),
            position.get("entry"),
            position.get("stop"),
        ),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonlStore:
    """
    Журнал JSONL с атомарной дозаписью и дедупликацией.

    Индекс id держится в памяти и восстанавливается из файла. Это
    компромисс: индекс на диске пришлось бы держать согласованным с
    журналом при обрыве, а перечитывание файла на старте даёт то же
    свойство без второго источника истины.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

        self._seen: set[str] = set()
        self._loaded = False
        self._corrupted = 0
        self._duplicates_skipped = 0

    # ------------------------------------------------------------ чтение

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        self._seen.clear()
        self._corrupted = 0

        if self.path.exists():
            for record in self._iter_raw():
                if record is None:
                    self._corrupted += 1
                    continue

                self._seen.add(observation_id(record))

        self._loaded = True

    def _iter_raw(self):
        # Отсутствующий журнал — это ШТАТНОЕ состояние (первый запуск, или
        # том подключён, но ещё пуст), а не ошибка чтения. Исключение
        # здесь ломало бы /performance/data-health ровно в том случае,
        # ради диагностики которого он и нужен.
        if not self.path.exists():
            return

        try:
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        yield None
                        continue

                    yield parsed if isinstance(parsed, dict) else None

        except OSError as error:
            raise StorageError(
                f"Unable to read {self.path}"
            ) from error

    def read_all(self) -> list[dict[str, Any]]:
        """Все корректные записи. Битые пропускаются и считаются."""
        records: list[dict[str, Any]] = []
        corrupted = 0

        for record in self._iter_raw():
            if record is None:
                corrupted += 1
                continue

            records.append(record)

        self._corrupted = corrupted

        return records

    @property
    def corrupted_count(self) -> int:
        self._ensure_loaded()
        return self._corrupted

    @property
    def duplicates_skipped(self) -> int:
        return self._duplicates_skipped

    def has(self, record_id: str) -> bool:
        self._ensure_loaded()
        return record_id in self._seen

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._seen)

    # ------------------------------------------------------------ запись

    def append(
        self,
        record: dict[str, Any],
        record_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Атомарно дописывает запись, если такой ещё не было.

        Возвращает результат явно (WRITTEN / DUPLICATE), а не молча
        игнорирует повтор: тихий пропуск невозможно отличить от потери.
        """
        self._ensure_loaded()

        identifier = record_id or observation_id(record)

        if identifier in self._seen:
            self._duplicates_skipped += 1

            return {
                "status": "DUPLICATE",
                "observation_id": identifier,
                "written": False,
            }

        stamped = dict(record)
        stamped.setdefault("observation_id", identifier)
        stamped.setdefault("stored_at_utc", utc_now())

        related_trade = trade_id(record)

        if related_trade is not None:
            stamped.setdefault("trade_id", related_trade)

        line = json.dumps(stamped, ensure_ascii=False, default=str) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Одна операция write на всю строку вместе с \n: частичная
            # строка не может попасть в файл при обрыве процесса.
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line)
                file.flush()
                os.fsync(file.fileno())

        except OSError as error:
            raise StorageError(
                f"Unable to append to {self.path}"
            ) from error

        self._seen.add(identifier)

        return {
            "status": "WRITTEN",
            "observation_id": identifier,
            "trade_id": stamped.get("trade_id"),
            "written": True,
        }


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """
    Атомарная запись JSON-состояния.

    Временный файл создаётся в ТОМ ЖЕ каталоге: os.replace атомарен
    только в пределах одной файловой системы, а /tmp может оказаться
    другой.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    handle, temporary = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name,
        suffix=".tmp",
    )

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, target)

    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass

        raise StorageError(f"Unable to write {target}") from error


def read_json(path: str | Path, default: Any = None) -> Any:
    """
    Читает JSON-состояние. Повреждённый файл НЕ роняет восстановление.

    Возвращается default: потерянное состояние означает «начать с
    известного безопасного значения», а не «упасть и не подняться».
    """
    target = Path(path)

    if not target.exists():
        return default

    try:
        with target.open("r", encoding="utf-8") as file:
            return json.load(file)

    except (json.JSONDecodeError, OSError):
        return default
