from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config.session_calendar import (
    SESSION_CALENDAR,
    SESSION_RESOLUTION_ORDER,
    parse_hhmm,
)


@dataclass
class SessionInfo:

    name: str
    local_time: datetime
    market_open: bool


class SessionResolver:

    @staticmethod
    def resolve(timestamp_ms: int) -> SessionInfo:
        """
        Определяет активную сессию по единому config/session_calendar.py.
        zoneinfo сам учитывает переход на летнее/зимнее время (DST) —
        например, America/New_York автоматически переключается между
        EST (UTC-5) и EDT (UTC-4) в правильные календарные даты.
        """

        utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=ZoneInfo("UTC"))

        for name in SESSION_RESOLUTION_ORDER:

            session = SESSION_CALENDAR[name]

            if session.synthetic:
                # CRYPTO: всегда "открыт" — это резервный вариант.
                return SessionInfo(name=name, local_time=utc, market_open=True)

            local_time = utc.astimezone(ZoneInfo(session.timezone))

            open_t = parse_hhmm(session.open_time)
            close_t = parse_hhmm(session.close_time)

            if open_t <= local_time.time() < close_t:
                return SessionInfo(name=name, local_time=local_time, market_open=True)

        # Не должно достигаться, т.к. CRYPTO synthetic всегда матчится последним,
        # но оставлено как явный безопасный fallback.
        return SessionInfo(name="CRYPTO", local_time=utc, market_open=True)
