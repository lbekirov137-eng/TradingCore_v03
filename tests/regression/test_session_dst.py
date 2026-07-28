"""
Proves the unified session calendar (config/session_calendar.py) is
DST-aware via zoneinfo, without any manual UTC-offset arithmetic.

US DST 2024: EDT (UTC-4) until 2024-11-03 02:00 local, then EST (UTC-5).
So NY market open (09:30 local) is:
  - 13:30 UTC while in EDT (summer)
  - 14:30 UTC while in EST (winter)
If SessionResolver used a fixed UTC offset, one of these two would
resolve to the wrong session.
"""

from datetime import datetime, timezone

from config.session_resolver import SessionResolver


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_new_york_session_open_edt_summer():
    # 2024-07-15 13:35 UTC == 09:35 EDT (summer, UTC-4) -> inside NY session
    dt = datetime(2024, 7, 15, 13, 35, tzinfo=timezone.utc)
    session = SessionResolver.resolve(_ms(dt))
    assert session.name == "NEW_YORK"
    assert session.market_open is True


def test_new_york_session_closed_at_same_utc_clock_time_in_winter():
    # Same UTC wall-clock time (13:35 UTC) in winter (EST, UTC-5) is only
    # 08:35 local -> BEFORE the 09:30 NY open. A fixed-UTC-offset
    # implementation would wrongly treat this identically to summer.
    dt = datetime(2024, 12, 15, 13, 35, tzinfo=timezone.utc)
    session = SessionResolver.resolve(_ms(dt))
    assert session.name != "NEW_YORK"


def test_new_york_session_open_est_winter_at_correct_utc_hour():
    # 2024-12-15 14:35 UTC == 09:35 EST (winter, UTC-5) -> inside NY session
    dt = datetime(2024, 12, 15, 14, 35, tzinfo=timezone.utc)
    session = SessionResolver.resolve(_ms(dt))
    assert session.name == "NEW_YORK"
    assert session.market_open is True


def test_london_session_bst_summer():
    # 2024-07-15 09:00 UTC == 10:00 BST (summer, UTC+1) -> inside London session,
    # and NOT yet inside NY session (09:00 UTC = 05:00 EDT).
    dt = datetime(2024, 7, 15, 9, 0, tzinfo=timezone.utc)
    session = SessionResolver.resolve(_ms(dt))
    assert session.name == "LONDON"


def test_crypto_fallback_outside_ny_and_london_hours():
    # 2024-01-10 02:00 UTC: outside both NY (14:30-21:00 UTC in winter) and
    # London (08:00-16:00 UTC) -> falls back to the synthetic CRYPTO session.
    dt = datetime(2024, 1, 10, 2, 0, tzinfo=timezone.utc)
    session = SessionResolver.resolve(_ms(dt))
    assert session.name == "CRYPTO"
    assert session.market_open is True


def test_dst_spring_forward_transition_2024():
    # US DST started 2024-03-10 02:00 local (clocks jump 02:00 -> 03:00).
    # 2024-03-11 (the day after) 13:35 UTC should already be EDT (09:35 local).
    dt = datetime(2024, 3, 11, 13, 35, tzinfo=timezone.utc)
    session = SessionResolver.resolve(_ms(dt))
    assert session.name == "NEW_YORK"
