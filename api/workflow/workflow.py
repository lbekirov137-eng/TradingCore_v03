from api.scheduler.scheduler import Scheduler


class Workflow:

    @staticmethod
    def run(context):

        """
        Единая точка запуска всей торговой системы.

        Любой запуск начинается отсюда:
        - REST API
        - n8n
        - Telegram
        - Scheduler
        - тестирование
        - облачный сервер
        """

        decision = Scheduler.tick(context)

        return decision