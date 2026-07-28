from config.session_resolver import SessionResolver
from api.strategy_engine.strategies.orb.session_open import SessionOpen


class OpeningRange:

    ORB_MINUTES = 5

    @staticmethod
    def calculate(context):

        market = context.visible_market

        # Недостаточно свечей
        if len(market.timestamps) < OpeningRange.ORB_MINUTES:
            return None

        session = SessionResolver.resolve(
            market.timestamps[-1]
        )

        if not session.market_open:
            return None

        start_index = SessionOpen.find_first_candle(
            context,
            session,
        )

        if start_index is None:
            return None

        end_index = start_index + OpeningRange.ORB_MINUTES

        if end_index > len(market.highs):
            return None

        orb_high = max(market.highs[start_index:end_index])
        orb_low = min(market.lows[start_index:end_index])

        return {
            "session": session.name,
            "start_index": start_index,
            "end_index": end_index,
            "timestamp": market.timestamps[start_index],
            "high": orb_high,
            "low": orb_low,
            "range": orb_high - orb_low,
        }