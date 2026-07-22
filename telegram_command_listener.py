from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIRECTORY = Path("data")
JOURNAL_FILE = DATA_DIRECTORY / "paper_runs.jsonl"
POSITION_FILE = DATA_DIRECTORY / "paper_position.json"
OFFSET_FILE = DATA_DIRECTORY / "telegram_update_offset.json"

POLL_SECONDS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_json(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temp_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp_path.replace(path)


def read_last_journal_record() -> dict[str, Any]:
    if not JOURNAL_FILE.exists():
        return {}

    try:
        with JOURNAL_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            lines = [
                line.strip()
                for line in file
                if line.strip()
            ]

        if not lines:
            return {}

        return json.loads(lines[-1])

    except Exception:
        return {}


def calculate_closed_balance() -> float:
    balance = 1000.0

    if not JOURNAL_FILE.exists():
        return balance

    unique_closed: dict[str, float] = {}

    try:
        with JOURNAL_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            for line in file:
                line = line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except Exception:
                    continue

                position_event = record.get(
                    "position_event",
                    {},
                )

                if not isinstance(
                    position_event,
                    dict,
                ):
                    continue

                position = position_event.get(
                    "position",
                    {},
                )

                if not isinstance(
                    position,
                    dict,
                ):
                    continue

                closed_at = position.get(
                    "closed_at_utc"
                )

                if not closed_at:
                    continue

                try:
                    pnl = float(
                        position.get(
                            "realized_pnl",
                            0.0,
                        )
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    pnl = 0.0

                unique_closed[str(closed_at)] = pnl

        return balance + sum(
            unique_closed.values()
        )

    except Exception:
        return balance


class TelegramCommandListener:
    def __init__(self) -> None:
        self.token = os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "",
        ).strip()

        self.chat_id = os.getenv(
            "TELEGRAM_CHAT_ID",
            "",
        ).strip()

        self.base_url = (
            f"https://api.telegram.org/bot"
            f"{self.token}"
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.token
            and self.chat_id
        )

    def send(self, text: str) -> bool:
        if not self.configured:
            return False

        try:
            payload = urllib.parse.urlencode(
                {
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": (
                        "true"
                    ),
                }
            ).encode("utf-8")

            request = urllib.request.Request(
                url=(
                    f"{self.base_url}"
                    "/sendMessage"
                ),
                data=payload,
                method="POST",
            )

            with urllib.request.urlopen(
                request,
                timeout=20,
            ) as response:
                result = json.loads(
                    response.read().decode(
                        "utf-8"
                    )
                )

            return bool(
                result.get("ok")
            )

        except Exception:
            return False

    def get_updates(
        self,
        *,
        offset: int,
    ) -> list[dict[str, Any]]:
        if not self.configured:
            return []

        try:
            query = urllib.parse.urlencode(
                {
                    "timeout": 20,
                    "offset": offset,
                    "allowed_updates": (
                        json.dumps(
                            ["message"]
                        )
                    ),
                }
            )

            url = (
                f"{self.base_url}"
                f"/getUpdates?{query}"
            )

            with urllib.request.urlopen(
                url,
                timeout=25,
            ) as response:
                result = json.loads(
                    response.read().decode(
                        "utf-8"
                    )
                )

            updates = result.get(
                "result",
                [],
            )

            return (
                updates
                if isinstance(
                    updates,
                    list,
                )
                else []
            )

        except Exception:
            return []

    @staticmethod
    def build_ping_message() -> str:
        return "\n".join(
            [
                "?? PONG",
                "",
                "Railway Cloud: ONLINE",
                (
                    "UTC: "
                    f"{utc_now()}"
                ),
                "",
                "REAL ORDERS: DISABLED",
            ]
        )

    @staticmethod
    def build_status_message() -> str:
        record = read_last_journal_record()
        position = load_json(
            POSITION_FILE
        )

        pipeline = record.get(
            "pipeline",
            {},
        )

        if not isinstance(
            pipeline,
            dict,
        ):
            pipeline = {}

        decision_data = pipeline.get(
            "decision",
            {},
        )

        if not isinstance(
            decision_data,
            dict,
        ):
            decision_data = {}

        strategy_data = pipeline.get(
            "strategy",
            {},
        )

        if not isinstance(
            strategy_data,
            dict,
        ):
            strategy_data = {}

        price = record.get(
            "market_price",
            "UNKNOWN",
        )

        signal = strategy_data.get(
            "signal",
            decision_data.get(
                "signal",
                "UNKNOWN",
            ),
        )

        decision = decision_data.get(
            "engine_decision",
            decision_data.get(
                "decision",
                "UNKNOWN",
            ),
        )

        position_status = position.get(
            "status",
            "NONE",
        )

        symbol = record.get(
            "symbol",
            position.get(
                "symbol",
                "BTCUSDT",
            ),
        )

        latest_time = record.get(
            "recorded_at_utc",
            "UNKNOWN",
        )

        balance = calculate_closed_balance()

        lines = [
            "?? TRADINGCORE STATUS",
            "",
            "Railway Cloud: ONLINE",
            f"Symbol: {symbol}",
            f"Last price: {price}",
            f"Signal: {signal}",
            f"Decision: {decision}",
            (
                "Position: "
                f"{position_status}"
            ),
            (
                "Paper balance: "
                f"${balance:.2f}"
            ),
            (
                "Last analysis UTC: "
                f"{latest_time}"
            ),
        ]

        if (
            isinstance(position, dict)
            and position.get("status")
            == "OPEN"
        ):
            lines.extend(
                [
                    "",
                    (
                        "Entry: "
                        f"{position.get('entry')}"
                    ),
                    (
                        "Stop: "
                        f"{position.get('stop')}"
                    ),
                    (
                        "TP1: "
                        f"{position.get('take_profit_1')}"
                    ),
                    (
                        "TP2: "
                        f"{position.get('take_profit_2')}"
                    ),
                    (
                        "Unrealized PNL: "
                        f"{position.get('unrealized_pnl')}"
                    ),
                ]
            )

        lines.extend(
            [
                "",
                "REAL ORDERS: DISABLED",
            ]
        )

        return "\n".join(lines)

    def handle_update(
        self,
        update: dict[str, Any],
    ) -> None:
        message = update.get(
            "message",
            {},
        )

        if not isinstance(
            message,
            dict,
        ):
            return

        chat = message.get(
            "chat",
            {},
        )

        if not isinstance(
            chat,
            dict,
        ):
            return

        incoming_chat_id = str(
            chat.get(
                "id",
                "",
            )
        )

        if (
            incoming_chat_id
            != self.chat_id
        ):
            return

        text = str(
            message.get(
                "text",
                "",
            )
        ).strip()

        command = (
            text.split()[0].lower()
            if text
            else ""
        )

        if command in {
            "/ping",
            "/ping@tradingcore",
        }:
            self.send(
                self.build_ping_message()
            )
            return

        if command in {
            "/status",
            "/status@tradingcore",
        }:
            self.send(
                self.build_status_message()
            )
            return

    def run_forever(self) -> None:
        DATA_DIRECTORY.mkdir(
            parents=True,
            exist_ok=True,
        )

        offset_state = load_json(
            OFFSET_FILE
        )

        try:
            offset = int(
                offset_state.get(
                    "offset",
                    0,
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            offset = 0

        print(
            "TELEGRAM COMMAND LISTENER STARTED"
        )
        print(
            "COMMANDS: /ping /status"
        )

        while True:
            updates = self.get_updates(
                offset=offset
            )

            for update in updates:
                update_id = update.get(
                    "update_id"
                )

                if isinstance(
                    update_id,
                    int,
                ):
                    offset = update_id + 1

                self.handle_update(
                    update
                )

                save_json(
                    OFFSET_FILE,
                    {
                        "offset": offset,
                        "updated_at_utc": (
                            utc_now()
                        ),
                    },
                )

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    TelegramCommandListener().run_forever()
