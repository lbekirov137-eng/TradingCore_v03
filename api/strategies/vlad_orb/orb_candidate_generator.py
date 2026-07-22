from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


class VladORBCandidateGenerator:
    """
    Vlad ORB candidate generator.

    Safety stage:
    - analyzes only fully closed 5-minute candles;
    - builds the 09:30-10:30 New York opening range;
    - searches for breakout and retest from 10:30 to 12:00;
    - creates a hypothetical candidate only;
    - does not modify risk, decision, or execution;
    - never sends real orders.
    """

    NAME = "Vlad ORB Candidate Generator"
    VERSION = "1.0.0"

    SESSION_TIMEZONE = "America/New_York"
    REQUIRED_INTERVAL = "5m"

    SESSION_OPEN_HOUR = 9
    SESSION_OPEN_MINUTE = 30

    RANGE_END_HOUR = 10
    RANGE_END_MINUTE = 30

    TRADE_WINDOW_END_HOUR = 12
    TRADE_WINDOW_END_MINUTE = 0

    REQUIRED_RANGE_CANDLES = 12

    # Maximum accepted distance from the ORB boundary
    # for a candle to count as a retest.
    RETEST_TOLERANCE_PERCENT = 0.001

    def process(
        self,
        market_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_market_snapshot(
            market_snapshot
        )

        interval = market_snapshot["interval"]

        if interval != self.REQUIRED_INTERVAL:
            return self._no_candidate(
                status="UNSUPPORTED_INTERVAL",
                reason=(
                    "Vlad ORB requires "
                    f"{self.REQUIRED_INTERVAL} candles"
                ),
            )

        open_times_ms = market_snapshot["open_times_ms"]
        opens = market_snapshot["opens"]
        highs = market_snapshot["highs"]
        lows = market_snapshot["lows"]
        closes = market_snapshot["closes"]

        ny_timezone = ZoneInfo(
            self.SESSION_TIMEZONE
        )

        candle_times = [
            datetime.fromtimestamp(
                timestamp_ms / 1000,
                timezone.utc,
            ).astimezone(ny_timezone)
            for timestamp_ms in open_times_ms
        ]

        latest_candle_time = candle_times[-1]
        session_date = latest_candle_time.date()

        session_open = datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            self.SESSION_OPEN_HOUR,
            self.SESSION_OPEN_MINUTE,
            tzinfo=ny_timezone,
        )

        range_end = datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            self.RANGE_END_HOUR,
            self.RANGE_END_MINUTE,
            tzinfo=ny_timezone,
        )

        trade_window_end = datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            self.TRADE_WINDOW_END_HOUR,
            self.TRADE_WINDOW_END_MINUTE,
            tzinfo=ny_timezone,
        )

        range_indices = [
            index
            for index, candle_time in enumerate(
                candle_times
            )
            if session_open <= candle_time < range_end
        ]

        if len(range_indices) < self.REQUIRED_RANGE_CANDLES:
            return self._no_candidate(
                status="BUILDING_RANGE",
                reason=(
                    "Opening range is not complete"
                ),
                session_date=str(session_date),
                candles_collected=len(range_indices),
                candles_required=self.REQUIRED_RANGE_CANDLES,
            )

        orb_high = max(
            highs[index]
            for index in range_indices
        )
        orb_low = min(
            lows[index]
            for index in range_indices
        )

        trade_indices = [
            index
            for index, candle_time in enumerate(
                candle_times
            )
            if range_end <= candle_time < trade_window_end
        ]

        if not trade_indices:
            return self._no_candidate(
                status="WAITING_FOR_BREAKOUT",
                reason=(
                    "No closed candles are available "
                    "inside the ORB trade window"
                ),
                session_date=str(session_date),
                orb_high=orb_high,
                orb_low=orb_low,
            )

        breakout = self._find_breakout(
            trade_indices=trade_indices,
            candle_times=candle_times,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            orb_high=orb_high,
            orb_low=orb_low,
        )

        if breakout is None:
            return self._no_candidate(
                status="NO_BREAKOUT",
                reason=(
                    "No candle closed outside "
                    "the opening range"
                ),
                session_date=str(session_date),
                orb_high=orb_high,
                orb_low=orb_low,
            )

        retest = self._find_retest(
            breakout=breakout,
            trade_indices=trade_indices,
            candle_times=candle_times,
            highs=highs,
            lows=lows,
            closes=closes,
            orb_high=orb_high,
            orb_low=orb_low,
        )

        if retest is None:
            return {
                **self._no_candidate(
                    status="WAITING_FOR_RETEST",
                    reason=(
                        "Breakout confirmed, but no "
                        "valid retest is available yet"
                    ),
                    session_date=str(session_date),
                    orb_high=orb_high,
                    orb_low=orb_low,
                ),
                "breakout": breakout,
            }

        candidate = self._build_candidate(
            breakout=breakout,
            retest=retest,
            orb_high=orb_high,
            orb_low=orb_low,
        )

        return {
            "generator": self.NAME,
            "version": self.VERSION,
            "status": "CANDIDATE_READY",
            "reason": None,
            "session_timezone": self.SESSION_TIMEZONE,
            "session_date": str(session_date),
            "range_start": session_open.isoformat(),
            "range_end": range_end.isoformat(),
            "trade_window_end": trade_window_end.isoformat(),
            "orb_high": orb_high,
            "orb_low": orb_low,
            "orb_size": orb_high - orb_low,
            "breakout": breakout,
            "retest": retest,
            "candidate": candidate,
            "signal": candidate["signal"],
            "real_order_sent": False,
        }

    def _find_breakout(
        self,
        *,
        trade_indices: list[int],
        candle_times: list[datetime],
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        orb_high: float,
        orb_low: float,
    ) -> dict[str, Any] | None:
        for index in trade_indices:
            close_price = closes[index]

            if close_price > orb_high:
                return {
                    "direction": "LONG",
                    "signal": "BUY",
                    "index": index,
                    "time": candle_times[index].isoformat(),
                    "open": opens[index],
                    "high": highs[index],
                    "low": lows[index],
                    "close": close_price,
                    "boundary": orb_high,
                }

            if close_price < orb_low:
                return {
                    "direction": "SHORT",
                    "signal": "SELL",
                    "index": index,
                    "time": candle_times[index].isoformat(),
                    "open": opens[index],
                    "high": highs[index],
                    "low": lows[index],
                    "close": close_price,
                    "boundary": orb_low,
                }

        return None

    def _find_retest(
        self,
        *,
        breakout: dict[str, Any],
        trade_indices: list[int],
        candle_times: list[datetime],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        orb_high: float,
        orb_low: float,
    ) -> dict[str, Any] | None:
        breakout_index = int(
            breakout["index"]
        )
        direction = breakout["direction"]

        boundary = (
            orb_high
            if direction == "LONG"
            else orb_low
        )

        tolerance = (
            boundary
            * self.RETEST_TOLERANCE_PERCENT
        )

        for index in trade_indices:
            if index <= breakout_index:
                continue

            if direction == "LONG":
                touched_boundary = (
                    lows[index]
                    <= boundary + tolerance
                )
                closed_valid_side = (
                    closes[index] >= boundary
                )
            else:
                touched_boundary = (
                    highs[index]
                    >= boundary - tolerance
                )
                closed_valid_side = (
                    closes[index] <= boundary
                )

            if touched_boundary and closed_valid_side:
                return {
                    "direction": direction,
                    "index": index,
                    "time": candle_times[index].isoformat(),
                    "high": highs[index],
                    "low": lows[index],
                    "close": closes[index],
                    "boundary": boundary,
                    "tolerance": tolerance,
                }

        return None

    @staticmethod
    def _build_candidate(
        *,
        breakout: dict[str, Any],
        retest: dict[str, Any],
        orb_high: float,
        orb_low: float,
    ) -> dict[str, Any]:
        direction = breakout["direction"]
        signal = breakout["signal"]
        entry = float(retest["close"])

        if direction == "LONG":
            stop = min(
                float(retest["low"]),
                orb_high,
            )
            risk_per_unit = entry - stop
            take_profit_2r = (
                entry + 2.0 * risk_per_unit
            )
            take_profit_3r = (
                entry + 3.0 * risk_per_unit
            )
        else:
            stop = max(
                float(retest["high"]),
                orb_low,
            )
            risk_per_unit = stop - entry
            take_profit_2r = (
                entry - 2.0 * risk_per_unit
            )
            take_profit_3r = (
                entry - 3.0 * risk_per_unit
            )

        if risk_per_unit <= 0:
            return {
                "status": "INVALID_CANDIDATE",
                "signal": "NO_TRADE",
                "direction": direction,
                "reason": (
                    "Calculated stop does not create "
                    "positive risk distance"
                ),
                "real_order_sent": False,
            }

        return {
            "status": "CANDIDATE",
            "strategy": "VLAD_ORB",
            "signal": signal,
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "take_profit_2r": take_profit_2r,
            "take_profit_3r": take_profit_3r,
            "risk_per_unit": risk_per_unit,
            "breakout_time": breakout["time"],
            "retest_time": retest["time"],
            "reason": (
                "Closed breakout outside ORB "
                "followed by valid retest"
            ),
            "real_order_sent": False,
        }

    def _no_candidate(
        self,
        *,
        status: str,
        reason: str,
        session_date: str | None = None,
        candles_collected: int | None = None,
        candles_required: int | None = None,
        orb_high: float | None = None,
        orb_low: float | None = None,
    ) -> dict[str, Any]:
        return {
            "generator": self.NAME,
            "version": self.VERSION,
            "status": status,
            "reason": reason,
            "session_timezone": self.SESSION_TIMEZONE,
            "session_date": session_date,
            "candles_collected": candles_collected,
            "candles_required": candles_required,
            "orb_high": orb_high,
            "orb_low": orb_low,
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
