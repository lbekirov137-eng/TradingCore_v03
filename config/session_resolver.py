from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass
class SessionInfo:

    name: str
    local_time: datetime
    market_open: bool


class SessionResolver:

    @staticmethod
    def resolve(timestamp_ms: int) -> SessionInfo:

        utc = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=ZoneInfo("UTC"),
        )

        new_york = utc.astimezone(
            ZoneInfo("America/New_York")
        )

        london = utc.astimezone(
            ZoneInfo("Europe/London")
        )

        if (
            (new_york.hour > 9 or (new_york.hour == 9 and new_york.minute >= 30))
            and new_york.hour < 16
        ):
            return SessionInfo(
                name="NEW_YORK",
                local_time=new_york,
                market_open=True,
            )

        if 8 <= london.hour < 16:
            return SessionInfo(
                name="LONDON",
                local_time=london,
                market_open=True,
            )

        return SessionInfo(
            name="CRYPTO",
            local_time=utc,
            market_open=True,
        )