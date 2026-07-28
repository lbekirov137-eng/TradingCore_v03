from datetime import datetime, timezone

from config.session_resolver import SessionResolver


class SessionRule:
    """
    Раньше здесь было отдельное, не связанное ни с чем захардкоженное
    окно "UTC 7-16" — третье по счёту, несогласованное определение
    "торговой сессии" в кодовой базе (см. F19 в AUTOTRADING_RISK_REGISTER.md).
    Теперь используется тот же единый календарь (config/session_calendar.py),
    что и SessionResolver/SessionOpen. Правило не подключено ни к одному
    вызывающему коду (как и раньше) — исправление устраняет рассинхронизацию
    определений, а не меняет поведение решений.
    """

    @staticmethod
    def evaluate(context):

        market = getattr(context, "market", None)
        timestamps = getattr(market, "timestamps", None) if market else None

        if timestamps:
            reference_ms = timestamps[-1]
        else:
            reference_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        session = SessionResolver.resolve(reference_ms)

        if session.market_open:
            return {
                "passed": True,
                "reason": f"Активная сессия: {session.name}.",
            }

        return {
            "passed": False,
            "reason": "Низкая активность рынка.",
        }
