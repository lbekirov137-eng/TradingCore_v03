from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config.session_calendar import get_session, parse_hhmm


class SessionOpen:
    """
    Находит индекс первой свечи текущей сессии по единому
    config/session_calendar.py (см. SessionResolver — та же таблица).

    Раньше здесь были захардкожены: NY 9:30, London 8:00, и (что было
    подтверждённой критичной находкой аудита, F3) CRYPTO безусловно
    возвращал индекс 0 — то есть Opening Range для крипты не был
    привязан ни к какой реальной границе сессии, а строился из первых
    свечей ЛЮБОГО загруженного скользящего окна данных. Теперь для
    CRYPTO используется явная, документированная синтетическая граница
    (полночь UTC, см. SessionDefinition.synthetic в session_calendar.py),
    а для NY/London — реальные локальные часы открытия из календаря.
    """

    @staticmethod
    def find_first_candle(context, session):

        market = context.visible_market

        if len(market.timestamps) == 0:
            return None

        definition = get_session(session.name)

        if definition is None:
            return None

        if definition.synthetic:
            session_start_ms = SessionOpen._synthetic_midnight_ms(
                market.timestamps[-1], definition.timezone,
            )

            for index, timestamp in enumerate(market.timestamps):
                if timestamp >= session_start_ms:
                    return index

            return None

        open_time = parse_hhmm(definition.open_time)
        tzinfo = ZoneInfo(definition.timezone)

        for index, timestamp in enumerate(market.timestamps):

            local_time = datetime.fromtimestamp(timestamp / 1000, tz=tzinfo)

            if local_time.time() >= open_time:
                return index

        return None

    @staticmethod
    def _synthetic_midnight_ms(reference_timestamp_ms: int, tz_name: str) -> int:

        tzinfo = ZoneInfo(tz_name)

        reference = datetime.fromtimestamp(reference_timestamp_ms / 1000, tz=tzinfo)

        midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)

        return int(midnight.timestamp() * 1000)
