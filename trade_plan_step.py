import math
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep
from api.trade_plan import TradePlan


class TradePlanStep(BaseStep):
    NAME = "Trade Plan Step"
    VERSION = "2.1.0"

    ALLOWED_SIGNALS = {
        "BUY",
        "SELL",
        "NO TRADE",
    }

    REQUIRED_PLAN_FIELDS = (
        "entry",
        "stop",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
    )

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.strategy, dict):
            raise TypeError(
                "TradePlanStep expected context.strategy to be dict"
            )

        signal = context.strategy.get("signal")

        if signal not in self.ALLOWED_SIGNALS:
            raise ValueError(
                f"TradePlanStep invalid strategy signal: {signal}"
            )

        if not isinstance(context.market, dict):
            raise TypeError(
                "TradePlanStep expected context.market to be dict"
            )

        if "price" not in context.market:
            raise ValueError(
                "TradePlanStep market price is missing"
            )

        price = context.market["price"]

        if not self._is_valid_number(price):
            raise ValueError(
                "TradePlanStep price must be a finite number"
            )

        if float(price) <= 0:
            raise ValueError(
                "TradePlanStep price must be greater than zero"
            )

        if not isinstance(context.indicators, dict):
            raise TypeError(
                "TradePlanStep expected context.indicators to be dict"
            )

        if "atr" not in context.indicators:
            raise ValueError(
                "TradePlanStep ATR indicator is missing"
            )

        atr_data = context.indicators["atr"]

        if not isinstance(atr_data, dict):
            raise TypeError(
                "TradePlanStep ATR indicator must be dict"
            )

        if "value" not in atr_data:
            raise ValueError(
                "TradePlanStep ATR value is missing"
            )

        atr = atr_data["value"]

        if not self._is_valid_number(atr):
            raise ValueError(
                "TradePlanStep ATR must be a finite number"
            )

        if float(atr) <= 0:
            raise ValueError(
                "TradePlanStep ATR must be greater than zero"
            )

        if not isinstance(context.risk, dict):
            raise TypeError(
                "TradePlanStep expected context.risk to be dict"
            )

        if "allowed" not in context.risk:
            raise ValueError(
                "TradePlanStep risk permission is missing"
            )

        if not isinstance(context.risk["allowed"], bool):
            raise TypeError(
                "TradePlanStep risk allowed must be bool"
            )

        if context.risk["allowed"]:
            if signal != "BUY":
                raise ValueError(
                    "TradePlanStep approved risk requires BUY signal"
                )

            for field_name in (
                "risk_amount",
                "position_size",
                "stop_distance",
            ):
                if field_name not in context.risk:
                    raise ValueError(
                        "TradePlanStep risk result missing field: "
                        f"{field_name}"
                    )

                value = context.risk[field_name]

                if not self._is_valid_number(value):
                    raise ValueError(
                        "TradePlanStep risk field "
                        f"'{field_name}' must be finite"
                    )

                if float(value) <= 0:
                    raise ValueError(
                        "TradePlanStep risk field "
                        f"'{field_name}' must be greater than zero"
                    )

    def process(self, context: MarketContext) -> MarketContext:
        signal = context.strategy["signal"]
        risk_allowed = context.risk["allowed"]

        if not risk_allowed:
            trade_plan = self._blocked_plan(
                signal=signal,
                reason=context.risk.get(
                    "reason",
                    "Risk was not approved",
                ),
            )
        else:
            price = float(context.market["price"])
            atr = float(context.indicators["atr"]["value"])

            trade_plan = TradePlan.build(
                signal=signal,
                price=price,
                atr=atr,
            )

            self._validate_trade_plan(trade_plan)

            trade_plan = {
                **trade_plan,
                "signal": signal,
                "allowed": True,
                "reason": "Trade plan created",
                "position_size": context.risk["position_size"],
                "risk_amount": context.risk["risk_amount"],
                "risk_percent": context.risk["risk_percent"],
                "execution_mode": context.risk["execution_mode"],
            }

        context.execution["trade_plan"] = trade_plan

        context.audit["trade_plan_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "allowed": trade_plan["allowed"],
            "reason": trade_plan["reason"],
        }

        return context

    def _blocked_plan(
        self,
        signal: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "signal": signal,
            "allowed": False,
            "reason": reason,
            "entry": None,
            "stop": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "risk_reward": "NO TRADE",
            "position_size": 0.0,
            "risk_amount": 0.0,
        }

    def _validate_trade_plan(
        self,
        trade_plan: Any,
    ) -> None:
        if not isinstance(trade_plan, dict):
            raise TypeError(
                "TradePlan.build() must return dict"
            )

        for field_name in self.REQUIRED_PLAN_FIELDS:
            if field_name not in trade_plan:
                raise ValueError(
                    "TradePlan result missing field: "
                    f"{field_name}"
                )

        for field_name in (
            "entry",
            "stop",
            "take_profit_1",
            "take_profit_2",
        ):
            value = trade_plan[field_name]

            if not self._is_valid_number(value):
                raise ValueError(
                    "TradePlan field "
                    f"'{field_name}' must be finite"
                )

        entry = float(trade_plan["entry"])
        stop = float(trade_plan["stop"])
        tp1 = float(trade_plan["take_profit_1"])
        tp2 = float(trade_plan["take_profit_2"])

        if not (stop < entry < tp1 < tp2):
            raise ValueError(
                "TradePlan BUY levels are inconsistent"
            )

        if trade_plan["risk_reward"] != "1:2 / 1:3":
            raise ValueError(
                "TradePlan risk reward must be 1:2 / 1:3"
            )

    @staticmethod
    def _is_valid_number(value: Any) -> bool:
        if isinstance(value, bool):
            return False

        if not isinstance(value, (int, float)):
            return False

        return math.isfinite(float(value))
