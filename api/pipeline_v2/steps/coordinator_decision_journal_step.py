from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep


class CoordinatorDecisionJournalStep(BaseStep):
    """
    Writes one structured JSONL record for every processed market candle.

    The journal explains the complete decision path:

    EMA / Vlad ORB
        -> Strategy Coordinator
        -> RiskStep
        -> TradePlanStep
        -> DecisionEngine
        -> PaperExecutionStep

    This step never sends real orders and never changes a trading decision.
    """

    NAME = "Coordinator Decision Journal Step"
    VERSION = "1.0.0"
    MODE = "OBSERVABILITY_ONLY"

    DEFAULT_JOURNAL_PATH = "data/coordinator_decisions.jsonl"

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.strategy, dict):
            raise TypeError(
                "CoordinatorDecisionJournalStep expected context.strategy to be dict"
            )

        if not isinstance(context.execution, dict):
            raise TypeError(
                "CoordinatorDecisionJournalStep expected context.execution to be dict"
            )

        if not isinstance(context.decision, dict):
            raise TypeError(
                "CoordinatorDecisionJournalStep expected context.decision to be dict"
            )

        runtime = context.execution.get("runtime", {})
        if (
            isinstance(runtime, dict)
            and runtime.get("real_orders_enabled") is True
        ):
            raise RuntimeError(
                "CoordinatorDecisionJournalStep refuses "
                "real_orders_enabled=True"
            )

    def process(self, context: MarketContext) -> MarketContext:
        record = self._build_record(context)

        journal_path = Path(
            os.getenv(
                "COORDINATOR_DECISION_JOURNAL_PATH",
                self.DEFAULT_JOURNAL_PATH,
            )
        )
        journal_path.parent.mkdir(parents=True, exist_ok=True)

        written = self._append_if_new(
            journal_path=journal_path,
            record=record,
        )

        context.audit["coordinator_decision_journal_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "mode": self.MODE,
            "journal_path": str(journal_path),
            "record_written": written,
            "candle_key": record["candle_key"],
            "final_reason": record["final_reason"],
            "real_order_sent": False,
        }

        return context

    def _build_record(self, context: MarketContext) -> dict[str, Any]:
        strategy = context.strategy
        execution = context.execution
        decision = context.decision

        orb_result = self._dict(
            strategy.get("vlad_orb_candidate")
        )
        orb_candidate = self._dict(
            orb_result.get("candidate")
        )
        coordinator = self._dict(
            strategy.get("strategy_coordinator")
        )
        selected_trade = self._dict(
            strategy.get("selected_trade")
        )

        risk = self._dict(execution.get("risk"))
        if not risk:
            risk = self._dict(execution.get("risk_result"))

        trade_plan = self._dict(execution.get("trade_plan"))
        paper_order = self._dict(execution.get("paper_order"))
        runtime = self._dict(execution.get("runtime"))

        market = self._dict(getattr(context, "market", {}))
        market_data = self._dict(
            getattr(context, "market_data", {})
        )

        candle_close_ms = self._last_number(
            market.get("close_times_ms")
        )
        if candle_close_ms is None:
            candle_close_ms = self._last_number(
                market_data.get("close_times_ms")
            )

        candle_close_utc = self._milliseconds_to_utc(
            candle_close_ms
        )

        recorded_at_utc = datetime.now(
            timezone.utc
        ).isoformat()

        candle_key = self._build_candle_key(
            symbol=getattr(context, "symbol", None),
            timeframe=getattr(context, "timeframe", None),
            candle_close_ms=candle_close_ms,
            recorded_at_utc=recorded_at_utc,
        )

        ema_signal = strategy.get(
            "signal",
            "UNKNOWN",
        )

        final_decision = decision.get(
            "decision",
            "UNKNOWN",
        )

        final_reason = self._final_reason(
            coordinator=coordinator,
            selected_trade=selected_trade,
            risk=risk,
            trade_plan=trade_plan,
            decision=decision,
            paper_order=paper_order,
        )

        return {
            "schema": "coordinator_decision_journal",
            "schema_version": "1.0",
            "recorded_at_utc": recorded_at_utc,
            "candle_close_utc": candle_close_utc,
            "candle_close_ms": candle_close_ms,
            "candle_key": candle_key,

            "exchange": getattr(
                context,
                "exchange",
                None,
            ),
            "symbol": getattr(
                context,
                "symbol",
                None,
            ),
            "timeframe": getattr(
                context,
                "timeframe",
                None,
            ),
            "price": self._find_price(
                context=context,
                market=market,
                market_data=market_data,
            ),

            "ema": {
                "signal": ema_signal,
                "reason": strategy.get(
                    "reason"
                ),
                "score": strategy.get(
                    "score"
                ),
            },

            "vlad_orb": {
                "status": orb_result.get(
                    "status",
                    "NOT_AVAILABLE",
                ),
                "signal": orb_result.get(
                    "signal",
                    "NO_TRADE",
                ),
                "reason": orb_result.get(
                    "reason"
                ),
                "candidate": orb_candidate or None,
            },

            "coordinator": {
                "version": coordinator.get(
                    "version"
                ),
                "mode": coordinator.get(
                    "mode"
                ),
                "selected_strategy": coordinator.get(
                    "selected_strategy",
                    selected_trade.get(
                        "strategy",
                        "NONE",
                    ),
                ),
                "selected_signal": coordinator.get(
                    "selected_signal",
                    selected_trade.get(
                        "signal",
                        "NO TRADE",
                    ),
                ),
                "status": coordinator.get(
                    "status",
                    "UNKNOWN",
                ),
                "reason": coordinator.get(
                    "reason",
                    selected_trade.get(
                        "reason",
                    ),
                ),
            },

            "selected_trade": selected_trade,

            "risk": {
                "approved": self._approved(
                    risk,
                    allowed_keys=(
                        "allowed",
                        "approved",
                        "risk_allowed",
                    ),
                ),
                "status": risk.get(
                    "status"
                ),
                "reason": risk.get(
                    "reason"
                ),
                "risk_amount": risk.get(
                    "risk_amount"
                ),
                "risk_percent": risk.get(
                    "risk_percent"
                ),
                "position_size": risk.get(
                    "position_size"
                ),
            },

            "trade_plan": {
                "approved": self._approved(
                    trade_plan,
                    allowed_keys=(
                        "allowed",
                        "approved",
                    ),
                ),
                "status": trade_plan.get(
                    "status"
                ),
                "reason": trade_plan.get(
                    "reason"
                ),
                "entry": trade_plan.get(
                    "entry"
                ),
                "stop": trade_plan.get(
                    "stop"
                ),
                "take_profit_1": trade_plan.get(
                    "take_profit_1"
                ),
                "take_profit_2": trade_plan.get(
                    "take_profit_2"
                ),
                "position_size": trade_plan.get(
                    "position_size"
                ),
                "execution_mode": trade_plan.get(
                    "execution_mode"
                ),
            },

            "decision_engine": {
                "decision": final_decision,
                "approved": final_decision == "TRADE",
                "reason": decision.get(
                    "reason"
                ),
            },

            "paper_execution": {
                "status": paper_order.get(
                    "status",
                    "NOT_AVAILABLE",
                ),
                "strategy": paper_order.get(
                    "strategy"
                ),
                "signal": paper_order.get(
                    "signal"
                ),
                "side": paper_order.get(
                    "side"
                ),
                "reason": paper_order.get(
                    "reason"
                ),
                "real_order_sent": False,
            },

            "final_reason": final_reason,

            "safety": {
                "execution_mode": runtime.get(
                    "execution_mode",
                    trade_plan.get(
                        "execution_mode",
                    ),
                ),
                "real_orders_enabled": False,
                "real_order_sent": False,
            },
        }

    @staticmethod
    def _final_reason(
        *,
        coordinator: dict[str, Any],
        selected_trade: dict[str, Any],
        risk: dict[str, Any],
        trade_plan: dict[str, Any],
        decision: dict[str, Any],
        paper_order: dict[str, Any],
    ) -> str:
        paper_status = paper_order.get("status")

        if paper_status == "FILLED_SIMULATED":
            return "PAPER_POSITION_OPENED"

        coordinator_status = coordinator.get("status")
        selected_signal = selected_trade.get(
            "signal",
            coordinator.get(
                "selected_signal",
                "NO TRADE",
            ),
        )

        if coordinator_status == "CONFLICT":
            return "EMA_ORB_CONFLICT"

        if (
            coordinator_status == "NO_CANDIDATE"
            or selected_signal not in {"BUY", "SELL"}
        ):
            return "NO_STRATEGY_CANDIDATE"

        if CoordinatorDecisionJournalStep._explicit_false(
            risk,
            keys=(
                "allowed",
                "approved",
                "risk_allowed",
            ),
        ):
            return "RISK_BLOCK"

        if CoordinatorDecisionJournalStep._explicit_false(
            trade_plan,
            keys=(
                "allowed",
                "approved",
            ),
        ):
            return "TRADE_PLAN_BLOCK"

        if decision.get("decision") != "TRADE":
            return "DECISION_ENGINE_BLOCK"

        if paper_status == "SKIPPED":
            return "PAPER_EXECUTION_SKIPPED"

        return "UNKNOWN_BLOCK"

    @staticmethod
    def _append_if_new(
        *,
        journal_path: Path,
        record: dict[str, Any],
    ) -> bool:
        previous_candle_key = (
            CoordinatorDecisionJournalStep._last_candle_key(
                journal_path
            )
        )

        candle_key = record.get("candle_key")

        if (
            previous_candle_key is not None
            and candle_key == previous_candle_key
        ):
            return False

        with journal_path.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        return True

    @staticmethod
    def _last_candle_key(
        journal_path: Path,
    ) -> str | None:
        if not journal_path.exists():
            return None

        try:
            with journal_path.open(
                "rb",
            ) as file:
                file.seek(
                    0,
                    os.SEEK_END,
                )
                position = file.tell()

                if position == 0:
                    return None

                buffer = bytearray()

                while position > 0:
                    position -= 1
                    file.seek(position)
                    character = file.read(1)

                    if character == b"\n" and buffer:
                        break

                    if character != b"\n":
                        buffer.extend(character)

                last_line = bytes(
                    reversed(buffer)
                ).decode(
                    "utf-8"
                )

            if not last_line.strip():
                return None

            previous_record = json.loads(
                last_line
            )
            value = previous_record.get(
                "candle_key"
            )
            return str(value) if value is not None else None

        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return None

    @staticmethod
    def _build_candle_key(
        *,
        symbol: Any,
        timeframe: Any,
        candle_close_ms: int | float | None,
        recorded_at_utc: str,
    ) -> str:
        if candle_close_ms is not None:
            return (
                f"{symbol or 'UNKNOWN'}:"
                f"{timeframe or 'UNKNOWN'}:"
                f"{int(candle_close_ms)}"
            )

        return (
            f"{symbol or 'UNKNOWN'}:"
            f"{timeframe or 'UNKNOWN'}:"
            f"{recorded_at_utc}"
        )

    @staticmethod
    def _find_price(
        *,
        context: MarketContext,
        market: dict[str, Any],
        market_data: dict[str, Any],
    ) -> float | None:
        direct_values = (
            getattr(context, "price", None),
            market.get("price"),
            market.get("last_price"),
            market_data.get("price"),
            market_data.get("last_price"),
        )

        for value in direct_values:
            number = CoordinatorDecisionJournalStep._number(
                value
            )
            if number is not None:
                return number

        for source in (
            market,
            market_data,
        ):
            closes = source.get("closes")
            if isinstance(closes, (list, tuple)) and closes:
                number = CoordinatorDecisionJournalStep._number(
                    closes[-1]
                )
                if number is not None:
                    return number

        return None

    @staticmethod
    def _milliseconds_to_utc(
        value: int | float | None,
    ) -> str | None:
        if value is None:
            return None

        try:
            return datetime.fromtimestamp(
                float(value) / 1000.0,
                tz=timezone.utc,
            ).isoformat()
        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            return None

    @staticmethod
    def _last_number(
        values: Any,
    ) -> float | None:
        if not isinstance(
            values,
            (list, tuple),
        ):
            return None

        if not values:
            return None

        return CoordinatorDecisionJournalStep._number(
            values[-1]
        )

    @staticmethod
    def _number(
        value: Any,
    ) -> float | None:
        if isinstance(value, bool):
            return None

        if isinstance(value, (int, float)):
            return float(value)

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _dict(
        value: Any,
    ) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _approved(
        value: dict[str, Any],
        *,
        allowed_keys: tuple[str, ...],
    ) -> bool | None:
        for key in allowed_keys:
            if key in value:
                result = value.get(key)
                if isinstance(result, bool):
                    return result

        return None

    @staticmethod
    def _explicit_false(
        value: dict[str, Any],
        *,
        keys: tuple[str, ...],
    ) -> bool:
        for key in keys:
            if key in value and value.get(key) is False:
                return True

        return False
