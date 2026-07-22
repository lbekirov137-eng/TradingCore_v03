from datetime import datetime, timezone
from typing import Any

from api.contracts.context import MarketContext
from api.decision_engine.rules.base_rule import BaseRule


class SessionRule(BaseRule):
    NAME = "Session Rule"
    VERSION = "2.1.0"

    ACTIVE_START_HOUR_UTC = 7
    ACTIVE_END_HOUR_UTC = 16

    DRY_RUN_MODE = "DRY_RUN"
    RUNTIME_FIELD = "runtime"
    MODE_FIELD = "mode"
    UTC_HOUR_OVERRIDE_FIELD = "utc_hour_override"

    def evaluate(
        self,
        context: MarketContext,
    ) -> dict[str, Any]:
        """
        Проверяет активную торговую сессию.

        В обычной работе всегда используется реальное время UTC.

        Подмена времени разрешается только при двух условиях:
        1. context.execution["runtime"]["mode"] == "DRY_RUN"
        2. задано поле utc_hour_override

        Это не позволяет случайно использовать тестовое время
        в реальной торговле.
        """

        hour = self._resolve_utc_hour(context)

        session_is_active = (
            self.ACTIVE_START_HOUR_UTC
            <= hour
            <= self.ACTIVE_END_HOUR_UTC
        )

        if session_is_active:
            return {
                "passed": True,
                "critical": False,
                "score": 15,
                "confidence": 0.95,
                "direction": None,
                "reason": "Активная торговая сессия",
            }

        return {
            "passed": False,
            "critical": True,
            "score": 0,
            "confidence": 1.0,
            "direction": None,
            "reason": "Вне активной торговой сессии",
        }

    def _resolve_utc_hour(
        self,
        context: MarketContext,
    ) -> int:
        """
        Возвращает час UTC.

        Для production используется реальное время.
        Для DRY_RUN допускается безопасная подмена часа.
        """

        runtime = self._get_runtime_settings(context)

        is_dry_run = (
            runtime.get(self.MODE_FIELD)
            == self.DRY_RUN_MODE
        )

        has_override = (
            self.UTC_HOUR_OVERRIDE_FIELD
            in runtime
        )

        if is_dry_run and has_override:
            override_hour = runtime[
                self.UTC_HOUR_OVERRIDE_FIELD
            ]

            self._validate_override_hour(
                override_hour
            )

            return int(override_hour)

        return datetime.now(timezone.utc).hour

    def _get_runtime_settings(
        self,
        context: MarketContext,
    ) -> dict[str, Any]:
        if not isinstance(context.execution, dict):
            return {}

        runtime = context.execution.get(
            self.RUNTIME_FIELD,
            {},
        )

        if not isinstance(runtime, dict):
            return {}

        return runtime

    @staticmethod
    def _validate_override_hour(
        hour: Any,
    ) -> None:
        if isinstance(hour, bool):
            raise TypeError(
                "SessionRule utc_hour_override must be int"
            )

        if not isinstance(hour, int):
            raise TypeError(
                "SessionRule utc_hour_override must be int"
            )

        if not 0 <= hour <= 23:
            raise ValueError(
                "SessionRule utc_hour_override "
                "must be between 0 and 23"
            )