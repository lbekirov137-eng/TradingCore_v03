from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


class TelegramNotifier:
    """
    Безопасный отправитель Telegram-уведомлений.

    Использует переменные окружения Railway:
    - TELEGRAM_BOT_TOKEN
    - TELEGRAM_CHAT_ID

    Ошибка Telegram никогда не должна останавливать PAPER Trading.
    """

    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.configured:
            return False

        url = (
            f"https://api.telegram.org/bot"
            f"{self.token}/sendMessage"
        )

        chunks = [
            text[index:index + 3900]
            for index in range(0, len(text), 3900)
        ]

        try:
            for chunk in chunks:
                payload = urllib.parse.urlencode(
                    {
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "disable_web_page_preview": "true",
                    }
                ).encode("utf-8")

                request = urllib.request.Request(
                    url=url,
                    data=payload,
                    method="POST",
                )

                with urllib.request.urlopen(
                    request,
                    timeout=20,
                ) as response:
                    result = json.loads(
                        response.read().decode("utf-8")
                    )

                if not result.get("ok"):
                    return False

            return True

        except Exception:
            return False

    @staticmethod
    def _money(value: Any) -> str:
        try:
            return f"{float(value):+.2f} USD"
        except (TypeError, ValueError):
            return "UNKNOWN"

    @staticmethod
    def _price(value: Any) -> str:
        try:
            return f"${float(value):.2f}"
        except (TypeError, ValueError):
            return "UNKNOWN"

    def notify_position_opened(
        self,
        *,
        position: dict[str, Any],
        ai_filter: dict[str, Any] | None = None,
    ) -> bool:
        ai_filter = (
            ai_filter
            if isinstance(ai_filter, dict)
            else {}
        )

        text = "\n".join(
            [
                "🟢 PAPER POSITION OPENED",
                "",
                (
                    f"{position.get('side', 'UNKNOWN')} "
                    f"{position.get('symbol', 'UNKNOWN')}"
                ),
                f"Вход: {self._price(position.get('entry'))}",
                f"Стоп: {self._price(position.get('stop'))}",
                (
                    "Take Profit 1: "
                    f"{self._price(position.get('take_profit_1'))}"
                ),
                (
                    "Take Profit 2: "
                    f"{self._price(position.get('take_profit_2'))}"
                ),
                (
                    "Риск: "
                    f"{self._money(position.get('risk_amount'))}"
                ),
                (
                    "Плечо: "
                    f"{position.get('leverage', 1)}x"
                ),
                "",
                (
                    "AI Filter: "
                    f"{ai_filter.get('decision', 'NOT_AVAILABLE')}"
                ),
                (
                    "AI Score: "
                    f"{ai_filter.get('score', 'N/A')}"
                ),
                "",
                "REAL ORDERS: DISABLED",
            ]
        )

        return self.send(text)

    def notify_tp1(
        self,
        *,
        position: dict[str, Any],
    ) -> bool:
        text = "\n".join(
            [
                "🟡 PAPER TP1 REACHED",
                "",
                (
                    f"{position.get('side', 'UNKNOWN')} "
                    f"{position.get('symbol', 'UNKNOWN')}"
                ),
                (
                    "Цена TP1: "
                    f"{self._price(position.get('take_profit_1'))}"
                ),
                (
                    "Текущая цена: "
                    f"{self._price(position.get('last_market_price'))}"
                ),
                (
                    "Плавающий PNL: "
                    f"{self._money(position.get('unrealized_pnl'))}"
                ),
                "",
                "Позиция продолжает сопровождаться.",
                "REAL ORDERS: DISABLED",
            ]
        )

        return self.send(text)

    def notify_position_closed(
        self,
        *,
        position: dict[str, Any],
        closed_balance: float | None = None,
        ai_filter: dict[str, Any] | None = None,
    ) -> bool:
        ai_filter = (
            ai_filter
            if isinstance(ai_filter, dict)
            else {}
        )

        pnl = position.get("realized_pnl")
        try:
            pnl_value = float(pnl)
        except (TypeError, ValueError):
            pnl_value = 0.0

        icon = "🟢" if pnl_value > 0 else "🔴" if pnl_value < 0 else "⚪"

        lines = [
            f"{icon} PAPER POSITION CLOSED",
            "",
            (
                f"{position.get('side', 'UNKNOWN')} "
                f"{position.get('symbol', 'UNKNOWN')}"
            ),
            f"Вход: {self._price(position.get('entry'))}",
            (
                "Выход: "
                f"{self._price(position.get('exit_price'))}"
            ),
            (
                "Причина: "
                f"{position.get('exit_reason', 'UNKNOWN')}"
            ),
            f"Результат: {self._money(pnl_value)}",
        ]

        if closed_balance is not None:
            lines.extend(
                [
                    (
                        "Закрытый баланс: "
                        f"${closed_balance:.2f}"
                    ),
                ]
            )

        lines.extend(
            [
                "",
                (
                    "AI Filter при входе: "
                    f"{ai_filter.get('decision', 'NOT_AVAILABLE')}"
                ),
                (
                    "AI Score: "
                    f"{ai_filter.get('score', 'N/A')}"
                ),
                "",
                "REAL ORDERS: DISABLED",
            ]
        )

        return self.send("\n".join(lines))

    def notify_error(
        self,
        *,
        error_type: str,
        error_message: str,
    ) -> bool:
        text = "\n".join(
            [
                "🚨 PAPER TRADING ERROR",
                "",
                f"Тип: {error_type}",
                f"Ошибка: {error_message}",
                "",
                "Система завершила цикл безопасно.",
                "REAL ORDERS: DISABLED",
            ]
        )

        return self.send(text)

    def notify_health(
        self,
        *,
        latest_event: str,
        latest_price: Any,
    ) -> bool:
        text = "\n".join(
            [
                "❤️ SYSTEM HEALTH: OK",
                "",
                "Railway: ONLINE",
                "Paper Trading: ACTIVE",
                "AI Observer: AVAILABLE",
                "AI Decision Filter: SHADOW MODE",
                f"Последнее событие: {latest_event}",
                f"Последняя цена: {self._price(latest_price)}",
                "",
                "REAL ORDERS: DISABLED",
            ]
        )

        return self.send(text)