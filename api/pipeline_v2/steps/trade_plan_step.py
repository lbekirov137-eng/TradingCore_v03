import math
from typing import Any

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep
from api.trade_plan import TradePlan


class TradePlanStep(BaseStep):
    NAME = "Trade Plan Step"
    VERSION = "4.1.0"

    ALLOWED_SIGNALS = {"BUY", "SELL", "NO TRADE"}
    PAPER_LONG_SHORT_MODE = "PAPER_LONG_SHORT"
    REQUIRED_PLAN_FIELDS = (
        "entry",
        "stop",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
    )

    # Максимальное расхождение между уровнем входа стратегии и текущей
    # рыночной ценой, выраженное в долях риска на единицу (R = |entry - stop|).
    #
    # Зачем нужен этот порог. Уровни VLAD_ORB вычисляются один раз за сессию
    # из ИСТОРИЧЕСКОЙ свечи ретеста (orb_candidate_generator: entry =
    # retest["close"]) и дальше не пересчитываются. Раньше план принимал их
    # без всякой сверки с рынком, поэтому вход исполнялся по цене, которой
    # на рынке давно нет: наблюдалось расхождение +273...+581 пункта при
    # риске 69.94 на единицу, то есть до 8R. Так как take_profit_2 = entry +
    # 3R, рынок оказывался уже ВЫШЕ цели, и позиция закрывалась мгновенно с
    # предопределённым результатом +3R на каждой свече.
    #
    # 0.5R выбрано как заведомо безопасное значение: оно допускает обычное
    # проскальзывание около уровня, но отсекает вход, при котором цель уже
    # достигнута рынком (для этого нужно >= 3R).
    ENTRY_DRIFT_TOLERANCE_R = 0.5

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

        # Защита в глубину для SPOT_LONG_ONLY.
        #
        # RiskStep уже блокирует SELL вне PAPER_LONG_SHORT, поэтому в
        # рабочем конвейере такая пара не встречается. Но полагаться на
        # единственную проверку нельзя: если риск окажется одобрен по
        # ошибке или в обход, шаг не должен строить короткий план в
        # режиме, где шорт запрещён.
        if (
            allowed
            and selected_trade.get("signal") == "SELL"
            and context.risk.get("execution_mode")
            != self.PAPER_LONG_SHORT_MODE
        ):
            raise ValueError(
                "TradePlanStep approved risk requires BUY signal outside "
                f"{self.PAPER_LONG_SHORT_MODE} mode"
            )

        # Рыночная цена и ATR проверяются ЗДЕСЬ, а не при использовании.
        #
        # Раньше отсутствие цены доходило до `float(context.market["price"])`
        # и вылетало голым KeyError — то есть шаг не отказывал, а падал.
        # Нулевой ATR ловился лишь косвенно, через несогласованные уровни
        # плана, и сообщение не указывало на настоящую причину.
        #
        # Цена нужна обеим веткам: ATR-плану для расчёта уровней и
        # VLAD_ORB-плану для проверки актуальности входа.
        if not isinstance(context.market, dict):
            raise TypeError("TradePlanStep expected context.market to be dict")

        price = context.market.get("price")
        if not self._is_positive_number(price):
            raise ValueError(
                "TradePlanStep market price is missing or not a positive "
                f"finite number: {price!r}"
            )

        if not isinstance(context.indicators, dict):
            raise TypeError(
                "TradePlanStep expected context.indicators to be dict"
            )

        atr_indicator = context.indicators.get("atr")
        if not isinstance(atr_indicator, dict):
            raise TypeError("TradePlanStep ATR indicator must be dict")

        atr = atr_indicator.get("value")
        if not self._is_positive_number(atr):
            raise ValueError(
                f"TradePlanStep ATR must be greater than zero: {atr!r}"
            )

    @staticmethod
    def _is_positive_number(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False

        number = float(value)

        return math.isfinite(number) and number > 0

    def process(self, context: MarketContext) -> MarketContext:
        selected_trade = context.strategy["selected_trade"]
        strategy = selected_trade.get("strategy", "NONE")
        signal = selected_trade["signal"]

        drift_reason = (
            self._entry_drift_reason(context, selected_trade)
            if strategy in {"VLAD_ORB", "EMA_AND_VLAD_ORB"}
            else None
        )

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
        elif (
            strategy in {"VLAD_ORB", "EMA_AND_VLAD_ORB"}
            and drift_reason is not None
        ):
            # Уровень входа устарел относительно рынка — торговать нельзя.
            # Отказ оформляется как обычный неразрешённый план (allowed=False),
            # а не как исключение: устаревший сигнал это штатная рыночная
            # ситуация, а не сбой пайплайна.
            plan = {
                "strategy": strategy,
                "signal": signal,
                "side": self._side(signal),
                "allowed": False,
                "reason": drift_reason,
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

    def _entry_drift_reason(
        self,
        context: MarketContext,
        selected_trade: Any,
    ) -> str | None:
        """
        Проверяет, что уровень входа ещё актуален для текущего рынка.

        Возвращает None, если вход допустим, иначе — причину отказа.

        Fail-closed: если цену или уровни невозможно прочитать как конечные
        числа, вход запрещается. Неизвестное состояние не считается
        безопасным — молча торговать по непроверяемому уровню хуже, чем
        пропустить сделку.
        """
        if not isinstance(selected_trade, dict):
            return "ENTRY_LEVEL_UNVERIFIABLE: selected_trade is not a dict"

        entry = self._finite(selected_trade.get("entry"))
        stop = self._finite(selected_trade.get("stop"))

        if entry is None or stop is None:
            return (
                "ENTRY_LEVEL_UNVERIFIABLE: entry/stop are not finite numbers"
            )

        market = context.market if isinstance(context.market, dict) else {}
        price = self._finite(market.get("price"))

        if price is None:
            return "ENTRY_LEVEL_UNVERIFIABLE: market price is not a finite number"

        risk_per_unit = abs(entry - stop)

        if risk_per_unit <= 0:
            return "ENTRY_LEVEL_UNVERIFIABLE: entry and stop coincide"

        drift = abs(price - entry)
        tolerance = self.ENTRY_DRIFT_TOLERANCE_R * risk_per_unit

        if drift > tolerance:
            return (
                "ENTRY_LEVEL_NO_LONGER_VALID: market price "
                f"{price} deviates from entry {entry} by {drift:.2f} "
                f"({drift / risk_per_unit:.2f}R), which exceeds the allowed "
                f"{self.ENTRY_DRIFT_TOLERANCE_R}R"
            )

        return None

    @staticmethod
    def _finite(value: Any) -> float | None:
        """Число, пригодное для сравнения цен, иначе None (bool отвергается)."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None

        number = float(value)

        return number if math.isfinite(number) else None

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
