"""
Глобальное расписание торговых сессий.
Все времена указаны в UTC.
"""


TRADING_SESSIONS = {

    "CRYPTO": {
        "enabled": True,
        "start": "00:00",
        "end": "23:59",
    },

    "LONDON": {
        "enabled": True,
        "start": "07:00",
        "end": "16:00",
    },

    "NEW_YORK": {
        "enabled": True,
        "start": "13:30",
        "end": "20:00",
    },

    "ASIA": {
        "enabled": True,
        "start": "00:00",
        "end": "09:00",
    },
}