from typing import Any

from api.contracts.context import MarketContext
from api.decision_engine.decision_engine import DecisionEngine
from api.pipeline_v2.steps.base_step import BaseStep


class DecisionStep(BaseStep):
    NAME = "Decision Step"
    VERSION = "4.0.0"

    ALLOWED_ENGINE_DECISIONS = {"TRADE", "NO_TRADE"}

    def process(self, context: MarketContext) -> MarketContext:
        context = DecisionEngine.process(context)

        if not isinstance(context, MarketContext):
            raise TypeError("DecisionEngine.process() must return MarketContext")

        self._validate_engine_decision(context.decision)

        engine_decision = context.decision["decision"]
        engine_reason = context.decision["reason"]
        selected_trade = context.strategy.get("selected_trade", {})
        signal = selected_trade.get("signal")
        strategy = selected_trade.get("strategy", "NONE")
        risk_allowed = context.risk.get("allowed") is True
        plan = context.execution.get("trade_plan", {})
        plan_allowed = isinstance(plan, dict) and plan.get("allowed") is True
        mode = context.risk.get("execution_mode", "SPOT_LONG_ONLY")

        blockers = []

        if signal not in {"BUY", "SELL"}:
            blockers.append(f"Selected signal is not BUY or SELL: {signal}")

        if signal == "SELL" and mode != "PAPER_LONG_SHORT":
            blockers.append("SELL is allowed only in PAPER_LONG_SHORT mode")

        if not risk_allowed:
            blockers.append("RiskStep did not approve the trade")

        if not plan_allowed:
            blockers.append("TradePlanStep did not approve the trade plan")

        if engine_decision != "TRADE":
            blockers.append(f"DecisionEngine blocked trade: {engine_reason}")

        final_decision = "NO_TRADE" if blockers else "TRADE"
        final_reason = (
            "; ".join(blockers)
            if blockers
            else "Selected strategy, risk, plan and rules approved"
        )

        context.decision = {
            **context.decision,
            "engine_decision": engine_decision,
            "decision": final_decision,
            "reason": final_reason,
            "strategy": strategy,
            "signal": signal,
            "side": self._side(signal),
            "risk_allowed": risk_allowed,
            "trade_plan_allowed": plan_allowed,
            "execution_mode": mode,
            "real_order_sent": False,
        }

        context.audit["decision_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "decision": final_decision,
            "reason": final_reason,
            "strategy": strategy,
            "signal": signal,
            "side": self._side(signal),
            "execution_mode": mode,
            "real_order_sent": False,
        }
        return context

    @staticmethod
    def _side(signal: str | None) -> str:
        return "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else "NONE"

    def _validate_engine_decision(self, decision: Any) -> None:
        if not isinstance(decision, dict):
            raise TypeError("DecisionEngine result must be dict")

        for field in ("decision", "score", "confidence", "failed_rules", "reason"):
            if field not in decision:
                raise ValueError(f"DecisionEngine missing field: {field}")

        if decision["decision"] not in self.ALLOWED_ENGINE_DECISIONS:
            raise ValueError("Invalid DecisionEngine decision")
