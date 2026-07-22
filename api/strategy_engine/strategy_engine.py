from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy


class StrategyEngine:

    @staticmethod
    def generate(context):

        signals = []

        orb = ORBStrategy.generate(context)

        signals.append(orb)

        return signals