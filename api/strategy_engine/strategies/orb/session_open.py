from datetime import time


class SessionOpen:

    @staticmethod
    def find_first_candle(context, session):

        market = context.visible_market

        for index, timestamp in enumerate(market.timestamps):

            local_time = session.local_time.__class__.fromtimestamp(
                timestamp / 1000,
                tz=session.local_time.tzinfo,
            )

            if session.name == "NEW_YORK":

                if local_time.time() >= time(9, 30):
                    return index

            elif session.name == "LONDON":

                if local_time.time() >= time(8, 0):
                    return index

            elif session.name == "CRYPTO":

                return 0

        return None