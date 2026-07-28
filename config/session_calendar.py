"""
Единый календарь торговых сессий — единственный источник истины.

До этого исправления в кодовой базе существовало ТРИ независимых и не
согласованных друг с другом определения "торговой сессии":
  - SessionResolver (захардкоженные часы NY/London + fallback CRYPTO),
  - config/trading_sessions.py (отдельный, нигде не используемый словарь),
  - SessionRule (захардкоженное окно UTC 7-16, не связанное ни с чем).
Это разночтение — подтверждённая находка аудита (см. AUTOTRADING_RISK_REGISTER.md, F19).

Здесь одно определение календаря, используемое SessionResolver и
SessionOpen. Часовые пояса заданы через IANA zoneinfo, поэтому переход
на летнее/зимнее время (DST) учитывается автоматически самим Python —
это НЕ нужно вычислять вручную (см. tests/regression/test_session_dst.py).

CRYPTO — синтетическая, явно документированная сессия: рынок торгует
24/7, поэтому "открытие сессии" для целей Opening Range Breakout
определяется как полночь UTC каждых суток. Это осознанное соглашение
(а не биржевой факт), которое можно переопределить конфигурацией ниже.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SessionDefinition:

    name: str
    timezone: str          # IANA zoneinfo key, e.g. "America/New_York"
    open_time: str          # "HH:MM" in the session's own local timezone
    close_time: str         # "HH:MM" in the session's own local timezone
    synthetic: bool = False  # True for CRYPTO: no real exchange enforces this boundary


# Единственный источник истины для всех модулей, работающих с сессиями.
SESSION_CALENDAR: dict[str, SessionDefinition] = {
    "NEW_YORK": SessionDefinition(
        name="NEW_YORK",
        timezone="America/New_York",
        open_time="09:30",
        close_time="16:00",
        synthetic=False,
    ),
    "LONDON": SessionDefinition(
        name="LONDON",
        timezone="Europe/London",
        open_time="08:00",
        close_time="16:00",
        synthetic=False,
    ),
    "CRYPTO": SessionDefinition(
        name="CRYPTO",
        timezone="UTC",
        open_time="00:00",
        close_time="23:59",
        synthetic=True,
    ),
}

# Порядок проверки при разрешении текущей сессии по timestamp.
# CRYPTO всегда последний — это резервный вариант "рынок торгует всегда".
SESSION_RESOLUTION_ORDER = ["NEW_YORK", "LONDON", "CRYPTO"]


def get_session(name: str) -> Optional[SessionDefinition]:
    return SESSION_CALENDAR.get(name)


def parse_hhmm(value: str):
    from datetime import time
    hour, minute = value.split(":")
    return time(int(hour), int(minute))
