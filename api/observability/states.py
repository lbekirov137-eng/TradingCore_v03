"""
Операторские состояния системы и структурированное логирование.

Логи НИКОГДА не содержат секретов: значения API-ключей не читаются
и не пишутся; в support bundle попадают только флаги наличия
переменных окружения, а не сами значения.
"""

import json
import os
import sys
import time
from enum import Enum
from typing import Optional


class SystemState(str, Enum):

    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    NO_TRADE = "NO_TRADE"
    SIGNAL_FOUND = "SIGNAL_FOUND"
    ORDER_PENDING = "ORDER_PENDING"
    OPENED = "OPENED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CLOSED = "CLOSED"
    RECONCILING = "RECONCILING"
    BLOCKED = "BLOCKED"
    FAILED_SAFELY = "FAILED_SAFELY"
    STOPPED = "STOPPED"


SECRET_KEY_FRAGMENTS = ("key", "secret", "token", "password", "signature", "sign")


def _redact(payload: dict) -> dict:
    """Заменяет любые потенциально секретные значения на маркер."""
    clean = {}
    for key, value in payload.items():
        if any(fragment in key.lower() for fragment in SECRET_KEY_FRAGMENTS):
            clean[key] = "***REDACTED***"
        elif isinstance(value, dict):
            clean[key] = _redact(value)
        else:
            clean[key] = value
    return clean


class StructuredLogger:

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.records = []

    def log(self, state: SystemState, message: str, **fields):

        record = {
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "state": state.value if isinstance(state, SystemState) else str(state),
            "message": message,
            **_redact(fields),
        }

        self.records.append(record)

        try:
            print(json.dumps(record, ensure_ascii=False, default=str), file=self.stream)
        except (OSError, ValueError):
            # Недоступный поток вывода не должен ронять торговый цикл.
            pass

        return record

    def recent(self, limit: int = 50):
        return self.records[-limit:]


logger = StructuredLogger()


class HealthTracker:
    """Отслеживает heartbeat, свежесть данных и время последней сверки."""

    def __init__(self):
        self.started_at = time.time()
        self.last_heartbeat = None
        self.last_market_data_timestamp = None
        self.last_successful_reconciliation = None
        self.last_error = None

    def heartbeat(self):
        self.last_heartbeat = time.time()

    def record_market_data(self, candle_timestamp_ms: int):
        self.last_market_data_timestamp = candle_timestamp_ms
        self.heartbeat()

    def record_reconciliation(self):
        self.last_successful_reconciliation = time.time()

    def record_error(self, message: str):
        self.last_error = {"message": message, "at": time.time()}

    def reset(self):
        """Только для тестов: обнуляет состояние глобального health-трекера."""
        self.started_at = time.time()
        self.last_heartbeat = None
        self.last_market_data_timestamp = None
        self.last_successful_reconciliation = None
        self.last_error = None

    def status(self) -> dict:
        now = time.time()

        data_age_seconds = None
        if self.last_market_data_timestamp is not None:
            data_age_seconds = round(now - (self.last_market_data_timestamp / 1000), 2)

        return {
            "uptime_seconds": round(now - self.started_at, 2),
            "last_heartbeat": self.last_heartbeat,
            "seconds_since_heartbeat": round(now - self.last_heartbeat, 2) if self.last_heartbeat else None,
            "last_market_data_timestamp": self.last_market_data_timestamp,
            "market_data_age_seconds": data_age_seconds,
            "last_successful_reconciliation": self.last_successful_reconciliation,
            "last_error": self.last_error,
        }


health = HealthTracker()


def build_support_bundle() -> dict:
    """
    Диагностический пакет для оператора БЕЗ секретов: только флаги
    наличия переменных окружения и агрегированное состояние.
    """

    env_flags = {}
    for name in ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET",
                  "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env_flags[name] = "set" if os.getenv(name) else "not_set"

    from config.settings import (
        PAPER_TRADING, LIVE_TRADING, DEFAULT_SYMBOL, DEFAULT_INTERVAL,
        DEFAULT_RISK_PERCENT, MIN_RISK_REWARD, MAX_DAILY_TRADES,
    )

    return {
        "environment": os.getenv("TRADING_ENVIRONMENT", "PAPER"),
        "env_variables_present": env_flags,
        "config": {
            "paper_trading": PAPER_TRADING,
            "live_trading": LIVE_TRADING,
            "symbol": DEFAULT_SYMBOL,
            "interval": DEFAULT_INTERVAL,
            "risk_percent": DEFAULT_RISK_PERCENT,
            "min_risk_reward": MIN_RISK_REWARD,
            "max_daily_trades": MAX_DAILY_TRADES,
        },
        "health": health.status(),
        "recent_logs": logger.recent(20),
    }
