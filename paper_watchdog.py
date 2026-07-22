from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIRECTORY = Path(__file__).resolve().parent

PAPER_LOOP_FILE = (
    BASE_DIRECTORY / "paper_live_loop.py"
)

TELEGRAM_LISTENER_FILE = (
    BASE_DIRECTORY / "telegram_command_listener.py"
)

RUNTIME_STATE_FILE = Path(
    os.getenv(
        "PAPER_RUNTIME_STATE_FILE",
        str(
            BASE_DIRECTORY
            / "data"
            / "paper_runtime_state.json"
        ),
    )
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv(
        "WATCHDOG_CHECK_INTERVAL_SECONDS",
        "30",
    )
)

STALE_AFTER_SECONDS = int(
    os.getenv(
        "WATCHDOG_STALE_SECONDS",
        "720",
    )
)

STARTUP_GRACE_SECONDS = int(
    os.getenv(
        "WATCHDOG_STARTUP_GRACE_SECONDS",
        "900",
    )
)

MAX_CONSECUTIVE_BAD_CHECKS = int(
    os.getenv(
        "WATCHDOG_MAX_BAD_CHECKS",
        "3",
    )
)


shutdown_requested = False


def utc_now() -> datetime:
    return datetime.now(
        timezone.utc
    )


def parse_utc(
    value: Any,
) -> datetime:
    text = str(value).strip()

    if text.endswith("Z"):
        text = (
            text[:-1] + "+00:00"
        )

    parsed = datetime.fromisoformat(
        text
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return parsed.astimezone(
        timezone.utc
    )


def request_shutdown(
    signum: int,
    _frame: object,
) -> None:
    global shutdown_requested

    shutdown_requested = True

    print(
        "WATCHDOG: shutdown signal "
        f"received: {signum}",
        flush=True,
    )


def read_heartbeat_age_seconds() -> float:
    with RUNTIME_STATE_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        state = json.load(file)

    updated_at_utc = state.get(
        "updated_at_utc"
    )

    if not updated_at_utc:
        raise RuntimeError(
            "updated_at_utc is missing "
            "from runtime state"
        )

    updated_at = parse_utc(
        updated_at_utc
    )

    age_seconds = (
        utc_now() - updated_at
    ).total_seconds()

    return max(
        0.0,
        age_seconds,
    )


def start_child(
    *,
    file_path: Path,
    name: str,
) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(file_path),
        ],
        cwd=str(
            BASE_DIRECTORY
        ),
    )

    print(
        f"WATCHDOG: {name} started, "
        f"PID={child.pid}",
        flush=True,
    )

    return child


def stop_child(
    *,
    child: subprocess.Popen[bytes],
    name: str,
) -> None:
    if child.poll() is not None:
        return

    print(
        f"WATCHDOG: stopping {name}...",
        flush=True,
    )

    child.terminate()

    try:
        child.wait(
            timeout=20
        )

    except subprocess.TimeoutExpired:
        print(
            "WATCHDOG: graceful stop "
            f"timed out for {name}; "
            "killing process",
            flush=True,
        )

        child.kill()
        child.wait(
            timeout=10
        )


def stop_all(
    *,
    paper_child: subprocess.Popen[bytes],
    telegram_child: subprocess.Popen[bytes],
) -> None:
    stop_child(
        child=telegram_child,
        name="Telegram command listener",
    )

    stop_child(
        child=paper_child,
        name="paper trading process",
    )


def main() -> int:
    required_files = [
        PAPER_LOOP_FILE,
        TELEGRAM_LISTENER_FILE,
    ]

    for required_file in required_files:
        if not required_file.exists():
            print(
                "WATCHDOG FATAL: "
                f"file not found: {required_file}",
                flush=True,
            )
            return 1

    signal.signal(
        signal.SIGTERM,
        request_shutdown,
    )

    signal.signal(
        signal.SIGINT,
        request_shutdown,
    )

    print(
        "=" * 70,
        flush=True,
    )
    print(
        "TRADING CORE - PAPER WATCHDOG",
        flush=True,
    )
    print(
        f"PAPER LOOP: {PAPER_LOOP_FILE}",
        flush=True,
    )
    print(
        "TELEGRAM LISTENER: "
        f"{TELEGRAM_LISTENER_FILE}",
        flush=True,
    )
    print(
        "HEARTBEAT FILE: "
        f"{RUNTIME_STATE_FILE}",
        flush=True,
    )
    print(
        "CHECK INTERVAL: "
        f"{CHECK_INTERVAL_SECONDS} seconds",
        flush=True,
    )
    print(
        "STALE LIMIT: "
        f"{STALE_AFTER_SECONDS} seconds",
        flush=True,
    )
    print(
        "STARTUP GRACE: "
        f"{STARTUP_GRACE_SECONDS} seconds",
        flush=True,
    )
    print(
        "REAL ORDERS: "
        "NOT ENABLED BY WATCHDOG",
        flush=True,
    )
    print(
        "=" * 70,
        flush=True,
    )

    paper_child = start_child(
        file_path=PAPER_LOOP_FILE,
        name="paper trading process",
    )

    telegram_child = start_child(
        file_path=TELEGRAM_LISTENER_FILE,
        name="Telegram command listener",
    )

    started_at = time.monotonic()
    consecutive_bad_checks = 0

    try:
        while True:
            if shutdown_requested:
                stop_all(
                    paper_child=paper_child,
                    telegram_child=telegram_child,
                )

                print(
                    "WATCHDOG STOPPED SAFELY",
                    flush=True,
                )
                return 0

            paper_exit_code = (
                paper_child.poll()
            )

            if paper_exit_code is not None:
                print(
                    "WATCHDOG FAILURE: "
                    "paper process exited "
                    f"with code {paper_exit_code}",
                    flush=True,
                )

                stop_all(
                    paper_child=paper_child,
                    telegram_child=telegram_child,
                )
                return 1

            telegram_exit_code = (
                telegram_child.poll()
            )

            if telegram_exit_code is not None:
                print(
                    "WATCHDOG FAILURE: "
                    "Telegram listener exited "
                    f"with code {telegram_exit_code}",
                    flush=True,
                )

                stop_all(
                    paper_child=paper_child,
                    telegram_child=telegram_child,
                )
                return 1

            running_seconds = (
                time.monotonic()
                - started_at
            )

            if (
                running_seconds
                < STARTUP_GRACE_SECONDS
            ):
                remaining = int(
                    STARTUP_GRACE_SECONDS
                    - running_seconds
                )

                print(
                    "WATCHDOG: startup grace "
                    "active; "
                    f"{remaining}s remaining",
                    flush=True,
                )

                time.sleep(
                    CHECK_INTERVAL_SECONDS
                )
                continue

            try:
                heartbeat_age = (
                    read_heartbeat_age_seconds()
                )

                print(
                    "WATCHDOG: heartbeat age "
                    f"{heartbeat_age:.1f}s",
                    flush=True,
                )

                if (
                    heartbeat_age
                    > STALE_AFTER_SECONDS
                ):
                    consecutive_bad_checks += 1

                    print(
                        "WATCHDOG WARNING: "
                        "stale heartbeat "
                        f"({consecutive_bad_checks}/"
                        f"{MAX_CONSECUTIVE_BAD_CHECKS})",
                        flush=True,
                    )
                else:
                    consecutive_bad_checks = 0

            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                json.JSONDecodeError,
                RuntimeError,
            ) as error:
                consecutive_bad_checks += 1

                print(
                    "WATCHDOG WARNING: "
                    "heartbeat read failed "
                    f"({consecutive_bad_checks}/"
                    f"{MAX_CONSECUTIVE_BAD_CHECKS}): "
                    f"{type(error).__name__}: "
                    f"{error}",
                    flush=True,
                )

            if (
                consecutive_bad_checks
                >= MAX_CONSECUTIVE_BAD_CHECKS
            ):
                print(
                    "WATCHDOG FAILURE: "
                    "trading loop is unhealthy. "
                    "Stopping container so "
                    "Railway can restart it.",
                    flush=True,
                )

                stop_all(
                    paper_child=paper_child,
                    telegram_child=telegram_child,
                )
                return 1

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

    except KeyboardInterrupt:
        stop_all(
            paper_child=paper_child,
            telegram_child=telegram_child,
        )

        print(
            "WATCHDOG STOPPED SAFELY",
            flush=True,
        )
        return 0

    finally:
        stop_all(
            paper_child=paper_child,
            telegram_child=telegram_child,
        )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
