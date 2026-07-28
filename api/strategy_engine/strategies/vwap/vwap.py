"""
Session VWAP — объёмно-взвешенная средняя цена, считаемая от начала
торговой сессии (а не от начала загруженного окна данных).

Сброс VWAP на границе сессии принципиален: VWAP, посчитанный от
произвольной точки скользящего окна, не имеет торгового смысла и
даёт разные значения на каждом тике для одной и той же свечи.
Начало сессии определяется единым календарём (config/session_calendar.py)
через SessionOpen — тот же источник истины, что и у ORB.
"""

from config.session_resolver import SessionResolver
from api.strategy_engine.strategies.orb.session_open import SessionOpen


def calculate_session_vwap(context):
    """
    Возвращает dict с VWAP по сессии и вспомогательными величинами,
    либо None, если данных недостаточно.

    Использует ТОЛЬКО видимые свечи (visible_market) — никакого
    заглядывания вперёд.
    """

    market = context.visible_market

    if len(market.timestamps) == 0:
        return None

    session = SessionResolver.resolve(market.timestamps[-1])

    start_index = SessionOpen.find_first_candle(context, session)

    if start_index is None:
        return None

    highs = market.highs[start_index:]
    lows = market.lows[start_index:]
    closes = market.closes[start_index:]
    volumes = market.volumes[start_index:]

    if not closes:
        return None

    cumulative_pv = 0.0
    cumulative_volume = 0.0

    for high, low, close, volume in zip(highs, lows, closes, volumes):
        typical_price = (high + low + close) / 3
        cumulative_pv += typical_price * volume
        cumulative_volume += volume

    if cumulative_volume <= 0:
        # Без объёма VWAP не определён — честно возвращаем None,
        # а не подменяем средней ценой.
        return None

    vwap = cumulative_pv / cumulative_volume

    last_close = closes[-1]

    deviations = [((h + l + c) / 3) - vwap for h, l, c in zip(highs, lows, closes)]
    mean_abs_deviation = sum(abs(d) for d in deviations) / len(deviations) if deviations else 0.0

    return {
        "vwap": vwap,
        "session": session.name,
        "start_index": start_index,
        "candles_in_session": len(closes),
        "last_close": last_close,
        "distance": last_close - vwap,
        "mean_abs_deviation": mean_abs_deviation,
    }
