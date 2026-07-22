from api.providers.base_provider import BaseProvider


class NewsProvider(BaseProvider):

    NAME = "News Provider"
    VERSION = "1.0.0"

    def fetch(self):

        """
        Пока заглушка.

        Позже здесь будут:
        - официальные новости бирж;
        - экономический календарь;
        - геополитика;
        - макроэкономика;
        """

        return []