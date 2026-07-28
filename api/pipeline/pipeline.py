from api.contracts.context import MarketContext

from api.pipeline.steps.indicator_step import IndicatorStep
from api.pipeline.steps.decision_step import DecisionStep


class MarketPipeline:

    NAME = "Market Pipeline"
    VERSION = "1.0.0"

    def __init__(self):
        self.steps = []

        self.register(IndicatorStep())
        self.register(DecisionStep())

    def register(self, step):
        self.steps.append(step)

    def run(self, context: MarketContext) -> MarketContext:

        for step in self.steps:
            context = step.process(context)

        return context