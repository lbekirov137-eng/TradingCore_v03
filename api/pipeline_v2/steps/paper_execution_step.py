import math
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep


class PaperExecutionStep(BaseStep):
    """Safe virtual BUY/SELL execution. Never sends real orders."""

    NAME = "Paper Execution Step"
    VERSION = "3.0.0"
    EXECUTION_MODE = "PAPER"

    REQUIRED_PLAN_FIELDS = (
        "entry",
        "stop",
        "take_profit_1",
        "take_profit_2",
        "position_size",
    )

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.decision, dict):
            raise TypeError("PaperExecutionStep decision must be dict")

        final_decision = context.decision.get("decision")
        if final_decision not in {"TRADE", "NO_TRADE"}:
            raise ValueError(f"Invalid final decision: {final_decision}")

        runtime = context.execution.get("runtime", {})
        if isinstance(runtime, dict) and runtime.get("real_orders_enabled") is True:
            raise RuntimeError(
                "PaperExecutionStep refuses real_orders_enabled=True"
            )

        if final_decision == "NO_TRADE":
            return

        selected_trade = context.strategy.get("selected_trade", {})
        signal = selected_trade.get("signal")
        if signal not in {"BUY", "SELL"}:
            raise ValueError("PaperExecutionStep supports BUY and SELL only")

        plan = context.execution.get("trade_plan")
        if not isinstance(plan, dict) or plan.get("allowed") is not True:
            raise ValueError("PaperExecutionStep trade plan is not allowed")

        if signal == "SELL" and plan.get("execution_mode") != "PAPER_LONG_SHORT":
            raise ValueError("Paper SELL requires PAPER_LONG_SHORT mode")

        for field in self.REQUIRED_PLAN_FIELDS:
            value = plan.get(field)
            if not self._positive(value):
                raise ValueError(f"Paper plan field {field} must be positive")

    def process(self, context: MarketContext) -> MarketContext:
        final_decision = context.decision["decision"]
        selected_trade = context.strategy.get("selected_trade", {})
        signal = selected_trade.get("signal")
        strategy = selected_trade.get("strategy", "NONE")

        if final_decision != "TRADE":
            order = {
                "mode": self.EXECUTION_MODE,
                "status": "SKIPPED",
                "strategy": strategy,
                "signal": signal,
                "side": self._side(signal),
                "real_order_sent": False,
                "reason": context.decision.get(
                    "reason",
                    "Final decision is NO_TRADE",
                ),
            }
            result = "SKIPPED"
        else:
            plan = context.execution["trade_plan"]
            order = {
                "mode": self.EXECUTION_MODE,
                "status": "FILLED_SIMULATED",
                "exchange": context.exchange,
                "symbol": context.symbol,
                "timeframe": context.timeframe,
                "strategy": strategy,
                "signal": signal,
                "side": self._side(signal),
                "entry": float(plan["entry"]),
                "quantity": float(plan["position_size"]),
                "stop": float(plan["stop"]),
                "take_profit_1": float(plan["take_profit_1"]),
                "take_profit_2": float(plan["take_profit_2"]),
                "risk_amount": plan.get("risk_amount"),
                "risk_percent": plan.get("risk_percent"),
                "execution_mode": plan.get(
                    "execution_mode",
                    "SPOT_LONG_ONLY",
                ),
                "real_order_sent": False,
                "reason": "Virtual paper order executed",
            }
            result = "FILLED_SIMULATED"

        context.execution["paper_order"] = order
        context.audit["paper_execution_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "mode": self.EXECUTION_MODE,
            "result": result,
            "strategy": strategy,
            "signal": signal,
            "side": self._side(signal),
            "execution_mode": order.get(
                "execution_mode",
                "SPOT_LONG_ONLY",
            ),
            "real_order_sent": False,
        }
        return context

    @staticmethod
    def _side(signal: str | None) -> str:
        return "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else "NONE"

    @staticmethod
    def _positive(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and float(value) > 0
        )
