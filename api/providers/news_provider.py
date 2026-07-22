from api.providers.base_provider import BaseProvider


class NewsProvider(BaseProvider):

    NAME = "News Provider"
    VERSION = "1.0.0"

    def fetch(self):

        """
        Пока заглушка.
        Позже здесь будет подключение
        официальных источников новостей.
        """

        return []