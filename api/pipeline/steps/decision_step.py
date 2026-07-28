from api.pipeline.steps.base_step import BaseStep
from api.contracts.context import MarketContext
from api.decision_engine.decision_engine import DecisionEngine


class DecisionStep(BaseStep):

    NAME = "Decision Step"
    VERSION = "1.1.0"

    def process(self, context: MarketContext) -> MarketContext:

        context.audit["decision_step"] = {
            "status": "RUNNING"
        }

        context = DecisionEngine.process(context)

        context.audit["decision_step"] = {
            "status": "OK"
        }

        return context