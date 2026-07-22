from __future__ import annotations

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep


class StrategyCoordinatorStep(BaseStep):
    """
    Paper-routing strategy coordinator.

    Selects EMA or Vlad ORB for downstream paper validation.
    It never sends real orders.
    """

    NAME = "Strategy Coordinator Step"
    VERSION = "2.0.0"
    MODE = "PAPER_ROUTING"

    RESULT_KEY = "strategy_coordinator"
    SELECTED_TRADE_KEY = "selected_trade"

    ALLOWED_SIGNALS = {"BUY", "SELL", "NO TRADE"}

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.strategy, dict):
            raise TypeError(
                "StrategyCoordinatorStep expected context.strategy to be dict"
            )

        primary_signal = context.strategy.get("signal")
        if primary_signal not in self.ALLOWED_SIGNALS:
            raise ValueError(
                f"Invalid primary strategy signal: {primary_signal}"
            )

        orb_result = context.strategy.get("vlad_orb_candidate")
        if orb_result is not None and not isinstance(orb_result, dict):
            raise TypeError("vlad_orb_candidate must be dict")

    def process(self, context: MarketContext) -> MarketContext:
        ema_signal = context.strategy.get("signal")
        orb_result = context.strategy.get("vlad_orb_candidate", {})
        orb_signal = orb_result.get("signal", "NO_TRADE")
        orb_status = orb_result.get("status", "NOT_AVAILABLE")
        orb_candidate = orb_result.get("candidate")

        decision = self._choose_strategy(
            ema_signal=ema_signal,
            orb_signal=orb_signal,
            orb_status=orb_status,
        )

        selected_trade = self._build_selected_trade(
            decision=decision,
            orb_candidate=orb_candidate,
        )

        context.strategy[self.RESULT_KEY] = {
            "coordinator": self.NAME,
            "version": self.VERSION,
            "mode": self.MODE,
            **decision,
            "real_order_sent": False,
        }
        context.strategy[self.SELECTED_TRADE_KEY] = selected_trade

        context.audit["strategy_coordinator_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "mode": self.MODE,
            "selected_strategy": decision["selected_strategy"],
            "selected_signal": decision["selected_signal"],
            "real_order_sent": False,
        }
        return context

    @staticmethod
    def _choose_strategy(
        *,
        ema_signal: str,
        orb_signal: str,
        orb_status: str,
    ) -> dict[str, str]:
        ema_active = ema_signal in {"BUY", "SELL"}
        orb_active = (
            orb_signal in {"BUY", "SELL"}
            and orb_status == "CANDIDATE_READY"
        )

        if ema_active and orb_active:
            if ema_signal == orb_signal:
                return {
                    "selected_strategy": "EMA_AND_VLAD_ORB",
                    "selected_signal": ema_signal,
                    "status": "AGREEMENT",
                    "reason": "EMA and Vlad ORB agree on direction",
                }
            return {
                "selected_strategy": "NONE",
                "selected_signal": "NO TRADE",
                "status": "CONFLICT",
                "reason": "EMA and Vlad ORB disagree on direction",
            }

        if orb_active:
            return {
                "selected_strategy": "VLAD_ORB",
                "selected_signal": orb_signal,
                "status": "SELECTED",
                "reason": "Vlad ORB has a valid candidate while EMA has no trade",
            }

        if ema_active:
            return {
                "selected_strategy": "EMA",
                "selected_signal": ema_signal,
                "status": "SELECTED",
                "reason": "EMA has a valid signal while Vlad ORB has no candidate",
            }

        return {
            "selected_strategy": "NONE",
            "selected_signal": "NO TRADE",
            "status": "NO_CANDIDATE",
            "reason": "Neither EMA nor Vlad ORB has a valid trade candidate",
        }

    @staticmethod
    def _build_selected_trade(
        *,
        decision: dict[str, str],
        orb_candidate,
    ) -> dict:
        strategy = decision["selected_strategy"]
        signal = decision["selected_signal"]

        if signal == "NO TRADE":
            return {
                "strategy": strategy,
                "signal": "NO TRADE",
                "entry": None,
                "stop": None,
                "take_profit_1": None,
                "take_profit_2": None,
                "risk_reward": "NO TRADE",
                "reason": decision["reason"],
                "real_order_sent": False,
            }

        if strategy in {"VLAD_ORB", "EMA_AND_VLAD_ORB"}:
            if not isinstance(orb_candidate, dict):
                raise ValueError("ORB candidate is required for selected Vlad ORB trade")
            if orb_candidate.get("status") != "CANDIDATE":
                raise ValueError("Selected ORB candidate is not valid")

            return {
                "strategy": strategy,
                "signal": signal,
                "entry": float(orb_candidate["entry"]),
                "stop": float(orb_candidate["stop"]),
                "take_profit_1": float(orb_candidate["take_profit_2r"]),
                "take_profit_2": float(orb_candidate["take_profit_3r"]),
                "risk_reward": "1:2 / 1:3",
                "reason": orb_candidate.get("reason", decision["reason"]),
                "real_order_sent": False,
            }

        return {
            "strategy": "EMA",
            "signal": signal,
            "entry": None,
            "stop": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "risk_reward": "1:2 / 1:3",
            "reason": decision["reason"],
            "real_order_sent": False,
        }
