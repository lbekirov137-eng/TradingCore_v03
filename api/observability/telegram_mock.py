"""
Mock-транспорт Telegram-уведомлений.

Ничего никуда не отправляет: сообщения складываются в память и
доступны через /observability/notifications. Это осознанно —
реальная отправка требует токена, а токены в этом проекте не
подключаются без явного решения пользователя.

Токен НИКОГДА не логируется: наличие проверяется как булев флаг.
"""

import os
import time


class TelegramMock:

    def __init__(self):
        self.messages = []

    def is_configured(self) -> bool:
        return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))

    def send(self, text: str, level: str = "INFO") -> dict:

        message = {
            "timestamp": time.time(),
            "level": level,
            "text": text,
            "delivered": False,
            "transport": "mock",
            "reason": (
                "Mock-транспорт: реальная отправка отключена. "
                "Настройте TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID и включите "
                "реальный транспорт явным решением."
            ),
        }

        self.messages.append(message)

        return message

    def recent(self, limit: int = 50):
        return self.messages[-limit:]

    def reset(self):
        self.messages.clear()


telegram = TelegramMock()
