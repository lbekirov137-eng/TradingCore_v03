from api.strategy_engine.strategies.orb.opening_range import OpeningRange
from api.strategy_engine.strategies.orb.breakout import Breakout
from api.strategy_engine.strategies.orb.retest import Retest
from api.strategy_engine.strategies.orb.confirmation import Confirmation
from api.strategy_engine.strategies.orb.cancel_scenario import CancelScenario
from api.strategy_engine.strategies.orb.entry import Entry
from api.strategy_engine.strategies.orb.stop_loss import StopLoss
from api.strategy_engine.strategies.orb.take_profit import TakeProfit
from api.strategy_engine.filters.regime import evaluate_all


class ORBStrategy:

    NAME = "ORB"

    # Фильтры режима/ликвидности можно отключить только явно (например,
    # в юнит-тестах отдельных компонентов) — по умолчанию они активны.
    APPLY_FILTERS = True

    @staticmethod
    def generate(context, apply_filters: bool = None):

        apply_filters = ORBStrategy.APPLY_FILTERS if apply_filters is None else apply_filters

        if apply_filters:
            filter_result = evaluate_all(context)
            if not filter_result.allowed:
                return ORBStrategy._no_trade(
                    filter_result.reason,
                    metadata={"regime": filter_result.regime},
                )

        opening_range = OpeningRange.calculate(context)

        if opening_range is None:
            return ORBStrategy._no_trade(
                "Недостаточно данных для Opening Range или сессия закрыта."
            )

        breakout = Breakout.detect(context, opening_range)
        retest = Retest.detect(context, opening_range, breakout)
        confirmation = Confirmation.check(context, breakout, retest)

        cancel = CancelScenario.check(
            context, opening_range, breakout, retest, confirmation
        )

        if cancel["cancel"]:
            return ORBStrategy._no_trade(
                cancel["reason"],
                metadata={"opening_range": opening_range},
            )

        entry = Entry.calculate(context, opening_range, breakout, confirmation)

        if entry is None:
            return ORBStrategy._no_trade(
                "Не удалось рассчитать точку входа.",
                metadata={"opening_range": opening_range},
            )

        atr = context.indicators.get("atr", {}).get("value")

        if atr is None or atr != atr or atr <= 0:
            return ORBStrategy._no_trade(
                "ATR недоступен или некорректен — недостаточно данных.",
                metadata={"opening_range": opening_range},
            )

        stop = StopLoss.calculate(context, opening_range, breakout)["stop"]

        risk_distance = abs(entry["entry"] - stop)

        if risk_distance <= 0:
            return ORBStrategy._no_trade(
                "Нулевое расстояние до стопа.",
                metadata={"opening_range": opening_range},
            )

        take_profit = TakeProfit.calculate(entry["entry"], stop, direction=breakout["direction"])

        return {
            "approved": True,
            "strategy": ORBStrategy.NAME,
            "direction": breakout["direction"],
            "trade_plan": {
                "entry": entry["entry"],
                "stop_loss": stop,
                "take_profit": take_profit,
                "risk_reward": take_profit["risk_reward"],
            },
            "confidence": breakout["strength"],
            "reason": confirmation["reason"],
            "metadata": {
                "session": opening_range["session"],
                "opening_range": opening_range,
            },
        }

    @staticmethod
    def _no_trade(reason, metadata=None):
        return {
            "approved": False,
            "strategy": ORBStrategy.NAME,
            "direction": None,
            "trade_plan": None,
            "confidence": 0.0,
            "reason": reason,
            "metadata": metadata or {},
        }
