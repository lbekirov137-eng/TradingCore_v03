import math
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep
from api.trade_plan import TradePlan


class TradePlanStep(BaseStep):
    NAME = "Trade Plan Step"
    VERSION = "4.0.0"

    ALLOWED_SIGNALS = {"BUY", "SELL", "NO TRADE"}
    REQUIRED_PLAN_FIELDS = (
        "entry",
        "stop",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
    )

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        selected_trade = context.strategy.get("selected_trade")
        if not isinstance(selected_trade, dict):
            raise TypeError("TradePlanStep selected_trade must be dict")

        signal = selected_trade.get("signal")
        if signal not in self.ALLOWED_SIGNALS:
            raise ValueError(f"Invalid selected signal: {signal}")

        if not isinstance(context.risk, dict):
            raise TypeError("TradePlanStep risk must be dict")

        allowed = context.risk.get("allowed")
        if not isinstance(allowed, bool):
            raise TypeError("TradePlanStep risk allowed must be bool")

    def process(self, context: MarketContext) -> MarketContext:
        selected_trade = context.strategy["selected_trade"]
        strategy = selected_trade.get("strategy", "NONE")
        signal = selected_trade["signal"]

        if not context.risk["allowed"]:
            plan = {
                "strategy": strategy,
                "signal": signal,
                "side": self._side(signal),
                "allowed": False,
                "reason": context.risk.get("reason", "Risk was not approved"),
                "entry": None,
                "stop": None,
                "take_profit_1": None,
                "take_profit_2": None,
                "risk_reward": "NO TRADE",
                "position_size": 0.0,
                "risk_amount": 0.0,
                "execution_mode": context.risk.get(
                    "execution_mode",
                    "SPOT_LONG_ONLY",
                ),
                "real_order_sent": False,
            }
        elif strategy in {"VLAD_ORB", "EMA_AND_VLAD_ORB"}:
            plan = {
                "entry": float(selected_trade["entry"]),
                "stop": float(selected_trade["stop"]),
                "take_profit_1": float(selected_trade["take_profit_1"]),
                "take_profit_2": float(selected_trade["take_profit_2"]),
                "risk_reward": selected_trade["risk_reward"],
            }
            self._validate_plan(plan, signal)
            plan = {
                **plan,
                "strategy": strategy,
                "signal": signal,
                "side": self._side(signal),
                "allowed": True,
                "reason": "Selected strategy trade plan accepted",
                "position_size": context.risk["position_size"],
                "risk_amount": context.risk["risk_amount"],
                "risk_percent": context.risk["risk_percent"],
                "execution_mode": context.risk["execution_mode"],
                "real_order_sent": False,
            }
        else:
            plan = TradePlan.build(
                signal=signal,
                price=float(context.market["price"]),
                atr=float(context.indicators["atr"]["value"]),
            )
            self._validate_plan(plan, signal)
            plan = {
                **plan,
                "strategy": strategy,
                "signal": signal,
                "side": self._side(signal),
                "allowed": True,
                "reason": "EMA ATR trade plan created",
                "position_size": context.risk["position_size"],
                "risk_amount": context.risk["risk_amount"],
                "risk_percent": context.risk["risk_percent"],
                "execution_mode": context.risk["execution_mode"],
                "real_order_sent": False,
            }

        context.execution["trade_plan"] = plan
        context.audit["trade_plan_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "allowed": plan["allowed"],
            "reason": plan["reason"],
            "strategy": strategy,
            "signal": signal,
            "side": self._side(signal),
            "execution_mode": plan["execution_mode"],
            "real_order_sent": False,
        }
        return context

    def _validate_plan(self, plan: Any, signal: str) -> None:
        if not isinstance(plan, dict):
            raise TypeError("Trade plan must return dict")

        for field in self.REQUIRED_PLAN_FIELDS:
            if field not in plan:
                raise ValueError(f"TradePlan missing field: {field}")

        entry = float(plan["entry"])
        stop = float(plan["stop"])
        tp1 = float(plan["take_profit_1"])
        tp2 = float(plan["take_profit_2"])

        valid = (
            stop < entry < tp1 < tp2
            if signal == "BUY"
            else tp2 < tp1 < entry < stop
        )
        if not valid:
            raise ValueError(f"TradePlan {signal} levels are inconsistent")

        if plan["risk_reward"] != "1:2 / 1:3":
            raise ValueError("TradePlan risk reward must be 1:2 / 1:3")

    @staticmethod
    def _side(signal: str) -> str:
        return "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else "NONE"
