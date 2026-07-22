from api.data_engine import DataEngine
from api.strategy_engine.strategy_engine import StrategyEngine
from api.decision_engine.decision_engine import DecisionEngine


class Scheduler:

    @staticmethod
    def tick(context):

        market = DataEngine.load(
            exchange=context.exchange,
            symbol=context.symbol,
            interval=context.interval,
            limit=context.limit,
        )

        context.market = market

        signals = StrategyEngine.generate(context)

        context.strategy_signals = signals

        decision = DecisionEngine.decide(context)

        return decision