import math
import os
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep
from api.risk_engine import RiskEngine


class RiskStep(BaseStep):
    NAME = "Risk Step"
    VERSION = "4.0.0"

    DEFAULT_BALANCE = 1000.0
    DEFAULT_RISK_PERCENT = 0.1
    MAX_RISK_PERCENT = 0.1

    DEFAULT_EXECUTION_MODE = "SPOT_LONG_ONLY"
    PAPER_LONG_SHORT_MODE = "PAPER_LONG_SHORT"

    ALLOWED_SIGNALS = {"BUY", "SELL", "NO TRADE"}
    ALLOWED_EXECUTION_MODES = {
        DEFAULT_EXECUTION_MODE,
        PAPER_LONG_SHORT_MODE,
    }

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.market, dict):
            raise TypeError("RiskStep expected context.market to be dict")

        price = context.market.get("price")
        if not self._is_valid_number(price) or float(price) <= 0:
            raise ValueError("RiskStep price must be a positive finite number")

        if not isinstance(context.indicators, dict):
            raise TypeError("RiskStep expected context.indicators to be dict")

        atr_data = context.indicators.get("atr")
        if not isinstance(atr_data, dict):
            raise TypeError("RiskStep ATR indicator must be dict")

        atr = atr_data.get("value")
        if not self._is_valid_number(atr) or float(atr) <= 0:
            raise ValueError("RiskStep ATR must be a positive finite number")

        if not isinstance(context.strategy, dict):
            raise TypeError("RiskStep expected context.strategy to be dict")

        selected_trade = context.strategy.get("selected_trade")
        if not isinstance(selected_trade, dict):
            raise TypeError("RiskStep selected_trade must be dict")

        signal = selected_trade.get("signal")
        if signal not in self.ALLOWED_SIGNALS:
            raise ValueError(f"RiskStep invalid selected signal: {signal}")

        if not isinstance(context.portfolio, dict):
            raise TypeError("RiskStep expected context.portfolio to be dict")

        balance = context.portfolio.get("balance", self.DEFAULT_BALANCE)
        risk_percent = context.portfolio.get(
            "risk_percent",
            self.DEFAULT_RISK_PERCENT,
        )

        if not self._is_valid_number(balance) or float(balance) <= 0:
            raise ValueError("RiskStep balance must be positive")

        if not self._is_valid_number(risk_percent):
            raise ValueError("RiskStep risk percent must be finite")

        risk_percent = float(risk_percent)
        if risk_percent <= 0 or risk_percent > self.MAX_RISK_PERCENT:
            raise ValueError("RiskStep risk percent must be within 0..0.1%")

        self._execution_mode(context)

    def process(self, context: MarketContext) -> MarketContext:
        selected_trade = context.strategy["selected_trade"]
        strategy = selected_trade.get("strategy", "NONE")
        signal = selected_trade["signal"]
        price = float(context.market["price"])
        atr = float(context.indicators["atr"]["value"])
        mode = self._execution_mode(context)

        balance = float(context.portfolio.get("balance", self.DEFAULT_BALANCE))
        risk_percent = float(
            context.portfolio.get("risk_percent", self.DEFAULT_RISK_PERCENT)
        )

        if signal == "NO TRADE":
            risk = self._blocked(
                "Coordinator returned NO TRADE",
                signal,
                balance,
                risk_percent,
                atr,
                mode,
                strategy,
            )
        elif signal == "SELL" and mode != self.PAPER_LONG_SHORT_MODE:
            risk = self._blocked(
                "SELL is disabled outside PAPER_LONG_SHORT mode",
                signal,
                balance,
                risk_percent,
                atr,
                mode,
                strategy,
            )
        elif strategy in {"VLAD_ORB", "EMA_AND_VLAD_ORB"}:
            risk = RiskEngine.calculate_by_stop(
                balance=balance,
                risk_percent=risk_percent,
                entry=float(selected_trade["entry"]),
                stop=float(selected_trade["stop"]),
            )
            self._validate_risk_result(risk)
            risk = {
                **risk,
                "reason": "Risk approved by selected strategy stop",
                "signal": signal,
                "side": self._side(signal),
                "strategy": strategy,
                "balance": round(balance, 2),
                "risk_percent": risk_percent,
                "execution_mode": mode,
                "real_order_sent": False,
            }
        else:
            risk = RiskEngine.calculate(
                balance=balance,
                risk_percent=risk_percent,
                price=price,
                atr=atr,
            )
            self._validate_risk_result(risk)
            risk = {
                **risk,
                "reason": "Risk approved by ATR",
                "signal": signal,
                "side": self._side(signal),
                "strategy": strategy,
                "balance": round(balance, 2),
                "risk_percent": risk_percent,
                "execution_mode": mode,
                "real_order_sent": False,
            }

        context.risk = risk
        context.audit["risk_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "allowed": risk["allowed"],
            "reason": risk["reason"],
            "signal": signal,
            "side": self._side(signal),
            "strategy": strategy,
            "execution_mode": mode,
            "real_order_sent": False,
        }
        return context

    def _execution_mode(self, context: MarketContext) -> str:
        mode = os.getenv(
            "TRADING_EXECUTION_MODE",
            self.DEFAULT_EXECUTION_MODE,
        ).strip().upper()

        if mode not in self.ALLOWED_EXECUTION_MODES:
            raise ValueError(f"Invalid TRADING_EXECUTION_MODE: {mode}")

        runtime = (
            context.execution.get("runtime", {})
            if isinstance(context.execution, dict)
            else {}
        )
        if (
            mode == self.PAPER_LONG_SHORT_MODE
            and runtime.get("real_orders_enabled") is True
        ):
            raise RuntimeError(
                "PAPER_LONG_SHORT cannot run with real orders enabled"
            )
        return mode

    def _blocked(
        self,
        reason: str,
        signal: str,
        balance: float,
        risk_percent: float,
        atr: float,
        mode: str,
        strategy: str,
    ) -> dict[str, Any]:
        return {
            "allowed": False,
            "reason": reason,
            "risk_amount": 0.0,
            "position_size": 0.0,
            "stop_distance": round(atr, 2),
            "signal": signal,
            "side": self._side(signal),
            "strategy": strategy,
            "balance": round(balance, 2),
            "risk_percent": risk_percent,
            "execution_mode": mode,
            "real_order_sent": False,
        }

    def _validate_risk_result(self, risk: Any) -> None:
        if not isinstance(risk, dict):
            raise TypeError("RiskEngine result must be dict")

        if not isinstance(risk.get("allowed"), bool):
            raise ValueError("RiskEngine result must contain bool allowed")

        for field in ("risk_amount", "position_size", "stop_distance"):
            value = risk.get(field)
            if not self._is_valid_number(value):
                raise ValueError(f"RiskEngine field {field} must be finite")
            if risk["allowed"] and float(value) <= 0:
                raise ValueError(f"RiskEngine field {field} must be positive")

    @staticmethod
    def _side(signal: str) -> str:
        return "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else "NONE"

    @staticmethod
    def _is_valid_number(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )
