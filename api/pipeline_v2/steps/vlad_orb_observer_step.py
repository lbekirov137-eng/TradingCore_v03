from __future__ import annotations

from api.contracts.context import MarketContext
from api.pipeline_v2.steps.base_step import BaseStep
from api.strategies.vlad_orb.orb_candidate_generator import (
    VladORBCandidateGenerator,
)
from api.strategies.vlad_orb.orb_observer import VladORBObserver


class VladORBObserverStep(BaseStep):
    """
    Read-only Vlad ORB analysis step.

    This step:
    - reads context.market;
    - runs VladORBObserver;
    - runs VladORBCandidateGenerator;
    - stores both results inside context.strategy;
    - preserves the primary strategy signal;
    - does not modify risk, decision, or execution;
    - never sends real orders.
    """

    NAME = "Vlad ORB Analysis Step"
    VERSION = "1.1.0"

    OBSERVER_RESULT_KEY = "vlad_orb_observer"
    CANDIDATE_RESULT_KEY = "vlad_orb_candidate"

    def __init__(self) -> None:
        self.observer = VladORBObserver()
        self.candidate_generator = VladORBCandidateGenerator()

    def validate(self, context: MarketContext) -> None:
        super().validate(context)

        if not isinstance(context.market, dict):
            raise TypeError(
                "VladORBObserverStep expected context.market to be dict"
            )

        if not isinstance(context.strategy, dict):
            raise TypeError(
                "VladORBObserverStep expected context.strategy to be dict"
            )

    def process(self, context: MarketContext) -> MarketContext:
        primary_signal_before = context.strategy.get("signal")

        observer_result = self.observer.process(context.market)
        candidate_result = self.candidate_generator.process(context.market)

        if not isinstance(observer_result, dict):
            raise TypeError(
                "VladORBObserver.process() must return dict"
            )

        if not isinstance(candidate_result, dict):
            raise TypeError(
                "VladORBCandidateGenerator.process() must return dict"
            )

        context.strategy[self.OBSERVER_RESULT_KEY] = observer_result
        context.strategy[self.CANDIDATE_RESULT_KEY] = candidate_result

        primary_signal_after = context.strategy.get("signal")

        if primary_signal_before != primary_signal_after:
            raise RuntimeError(
                "VladORBObserverStep changed the primary strategy signal"
            )

        context.audit["vlad_orb_observer_step"] = {
            "status": "OK",
            "version": self.VERSION,
            "observer_version": observer_result.get("version"),
            "candidate_generator_version": candidate_result.get("version"),
            "orb_status": observer_result.get("status"),
            "range_built": observer_result.get("range_built"),
            "candidate_status": candidate_result.get("status"),
            "candidate_signal": candidate_result.get("signal"),
            "signal_preserved": primary_signal_after,
            "real_order_sent": False,
        }

        return context
