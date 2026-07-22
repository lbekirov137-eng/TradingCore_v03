from api.contracts.context import MarketContext
from api.decision_engine.rules.base_rule import BaseRule
from api.providers.news_provider import NewsProvider


class NewsRule(BaseRule):

    NAME = "News Rule"
    VERSION = "1.0.0"

    def evaluate(self, context: MarketContext) -> dict:

        provider = NewsProvider()

        news = provider.fetch()

        # Пока новостей нет — правило пропускает торговлю.
        if len(news) == 0:
            return {
                "passed": True,
                "critical": False,
                "score": 10,
                "confidence": 0.80,
                "direction": None,
                "reason": "Новостей, блокирующих торговлю, не обнаружено.",
            }

        # Позже здесь будет полноценный анализ влияния новостей.
        return {
            "passed": False,
            "critical": True,
            "score": 0,
            "confidence": 1.0,
            "direction": None,
            "reason": "Обнаружены важные новости.",
        }