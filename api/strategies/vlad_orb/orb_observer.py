from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


class VladORBObserver:
    """
    Vlad Opening Range Breakout (ORB) observer.

    Stage 1:
    - observer only;
    - no trading signals;
    - no orders;
    - builds the first 60 minutes after 09:30 New York time
      from fully closed 5-minute candles.
    """

    NAME = "Vlad ORB Observer"
    VERSION = "1.1.0"

    SESSION_TIMEZONE = "America/New_York"
    SESSION_OPEN_HOUR = 9
    SESSION_OPEN_MINUTE = 30
    RANGE_MINUTES = 60
    REQUIRED_INTERVAL = "5m"
    REQUIRED_CANDLES = 12

    def process(
        self,
        market_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_market_snapshot(
            market_snapshot
        )

        interval = market_snapshot["interval"]

        if interval != self.REQUIRED_INTERVAL:
            return self._base_result(
                status="UNSUPPORTED_INTERVAL",
                reason=(
                    "Vlad ORB Observer requires "
                    f"{self.REQUIRED_INTERVAL} candles"
                ),
            )

        open_times_ms = market_snapshot[
            "open_times_ms"
        ]
        opens = market_snapshot["opens"]
        highs = market_snapshot["highs"]
        lows = market_snapshot["lows"]
        closes = market_snapshot["closes"]

        ny_timezone = ZoneInfo(
            self.SESSION_TIMEZONE
        )

        latest_open_dt = datetime.fromtimestamp(
            open_times_ms[-1] / 1000,
            timezone.utc,
        ).astimezone(ny_timezone)

        session_date = latest_open_dt.date()

        session_open_dt = datetime(
            year=session_date.year,
            month=session_date.month,
            day=session_date.day,
            hour=self.SESSION_OPEN_HOUR,
            minute=self.SESSION_OPEN_MINUTE,
            tzinfo=ny_timezone,
        )

        range_end_dt = session_open_dt.replace(
            hour=10,
            minute=30,
        )

        range_indices: list[int] = []

        for index, timestamp_ms in enumerate(
            open_times_ms
        ):
            candle_open_dt = (
                datetime.fromtimestamp(
                    timestamp_ms / 1000,
                    timezone.utc,
                ).astimezone(ny_timezone)
            )

            if (
                session_open_dt
                <= candle_open_dt
                < range_end_dt
            ):
                range_indices.append(index)

        candles_collected = len(range_indices)

        if candles_collected == 0:
            return self._base_result(
                status="WAITING_FOR_SESSION",
                reason=(
                    "No closed candles are available "
                    "from the 09:30 New York session"
                ),
                session_date=str(session_date),
                candles_collected=0,
                candles_required=self.REQUIRED_CANDLES,
            )

        orb_high = max(
            highs[index]
            for index in range_indices
        )
        orb_low = min(
            lows[index]
            for index in range_indices
        )

        first_index = range_indices[0]
        last_index = range_indices[-1]

        first_open_dt = datetime.fromtimestamp(
            open_times_ms[first_index] / 1000,
            timezone.utc,
        ).astimezone(ny_timezone)

        last_open_dt = datetime.fromtimestamp(
            open_times_ms[last_index] / 1000,
            timezone.utc,
        ).astimezone(ny_timezone)

        range_built = (
            candles_collected
            >= self.REQUIRED_CANDLES
        )

        status = (
            "RANGE_READY"
            if range_built
            else "BUILDING_RANGE"
        )

        return {
            "observer": self.NAME,
            "version": self.VERSION,
            "status": status,
            "reason": None,
            "session_timezone": (
                self.SESSION_TIMEZONE
            ),
            "session_date": str(session_date),
            "session_open": (
                session_open_dt.isoformat()
            ),
            "range_end": (
                range_end_dt.isoformat()
            ),
            "interval": interval,
            "candles_collected": (
                candles_collected
            ),
            "candles_required": (
                self.REQUIRED_CANDLES
            ),
            "range_built": range_built,
            "first_candle_open": (
                first_open_dt.isoformat()
            ),
            "last_candle_open": (
                last_open_dt.isoformat()
            ),
            "opening_price": (
                opens[first_index]
            ),
            "last_range_close": (
                closes[last_index]
            ),
            "orb_high": orb_high,
            "orb_low": orb_low,
            "orb_size": orb_high - orb_low,
            "breakout": None,
            "retest": None,
            "candidate": None,
            "signal": "NO_TRADE",
            "real_order_sent": False,
        }

    def _base_result(
        self,
        *,
        status: str,
        reason: str,
        session_date: str | None = None,
        candles_collected: int = 0,
        candles_required: int | None = None,
    ) -> dict[str, Any]:
        return {
            "observer": self.NAME,
            "version": self.VERSION,
            "status": status,
            "reason": reason,
            "session_timezone": (
                self.SESSION_TIMEZONE
            ),
            "session_date": session_date,
            "interval": None,
            "candles_collected": (
                candles_collected
            ),
            "candles_required": (
                candles_required
                if candles_required is not None
                else self.REQUIRED_CANDLES
            ),
            "range_built": False,
            "orb_high": None,
            "orb_low": None,
            "orb_size": None,
            "breakout": None,
            "retest": None,
            "candidate": None,
            "signal": "NO_TRADE",
            "real_order_sent": False,
        }

    @staticmethod
    def _validate_market_snapshot(
        market_snapshot: Any,
    ) -> None:
        if not isinstance(
            market_snapshot,
            dict,
        ):
            raise TypeError(
                "market_snapshot must be dict"
            )

        required_fields = (
            "interval",
            "open_times_ms",
            "opens",
            "highs",
            "lows",
            "closes",
        )

        missing_fields = [
            field
            for field in required_fields
            if field not in market_snapshot
        ]

        if missing_fields:
            raise ValueError(
                "market_snapshot is missing fields: "
                + ", ".join(missing_fields)
            )

        lengths = {
            len(market_snapshot[field])
            for field in (
                "open_times_ms",
                "opens",
                "highs",
                "lows",
                "closes",
            )
        }

        if len(lengths) != 1:
            raise ValueError(
                "market candle arrays must have "
                "equal lengths"
            )

        if not lengths or next(iter(lengths)) == 0:
            raise ValueError(
                "market candle arrays must not "
                "be empty"
            )
