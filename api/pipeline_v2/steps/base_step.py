from abc import ABC, abstractmethod

from api.contracts.context import MarketContext


class BaseStep(ABC):
    """
    Base contract for every executable pipeline step.

    Required lifecycle:
        execute(context)
            -> validate(context)
            -> process(context)
            -> MarketContext
    """

    def validate(self, context: MarketContext) -> None:
        """
        Validate the shared context before processing.
        Child classes may extend this method.
        """
        if not isinstance(context, MarketContext):
            raise TypeError(
                f"{self.__class__.__name__} expected MarketContext, "
                f"got {type(context).__name__}"
            )

    @abstractmethod
    def process(self, context: MarketContext) -> MarketContext:
        """
        Execute module-specific business logic.
        Must return MarketContext.
        """
        raise NotImplementedError

    def execute(self, context: MarketContext) -> MarketContext:
        """
        Public and mandatory entry point for pipeline execution.
        """
        self.validate(context)

        updated_context = self.process(context)

        if not isinstance(updated_context, MarketContext):
            raise TypeError(
                f"{self.__class__.__name__}.process() must return "
                f"MarketContext, got {type(updated_context).__name__}"
            )

        return updated_context