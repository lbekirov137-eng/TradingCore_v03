import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from api.contracts.context import MarketContext
from api.core.bootstrap import Bootstrap


# ---------------------------------------------------------------------------
# Синтетический рынок для dry-run и тестов.
#
# ПОЧЕМУ ПРЕЖНИЙ РЯД БЫЛ НЕРЕАЛИСТИЧНЫМ
#
# Раньше ряд строился формулой 100 + i*0.2 + 2*sin(i*0.7) на 250 свечей:
# монотонный рост примерно от 100 до 148 без какой-либо рыночной структуры.
# Опорный диапазон ORB (09:30-10:30 Нью-Йорк) попадал в самое начало, а
# последняя цена оказывалась почти на 50 пунктов выше него.
#
# В результате уровень входа, который vlad_orb берёт из свечи ретеста
# (orb_candidate_generator: entry = retest["close"]), отставал от рынка на
# 4.69R. Иначе говоря, фикстура моделировала УСТАРЕВШИЙ сигнал: вход по
# цене, которой на рынке уже нет. Пока в пайплайне не было проверки
# актуальности, такой вход принимался, поэтому тесты проходили — и ровно
# этот же дефект наблюдался в облаке, где сделки закрывались с
# предопределённым результатом +3R.
#
# ПОЧЕМУ НОВЫЙ РЯД СООТВЕТСТВУЕТ РЕАЛЬНОМУ СЦЕНАРИЮ
#
# Теперь ряд воспроизводит канонический сетап ORB целиком:
#   1) фон          — многочасовой умеренный рост до сессии;
#   2) диапазон     — 12 свечей 09:30-10:30 NY колеблются внутри коридора,
#                     формируя orb_high / orb_low;
#   3) пробой       — первая свеча торгового окна закрывается выше orb_high;
#   4) удержание    — цена держится выше границы, не возвращаясь к ней;
#   5) ретест       — свеча откатывается к orb_high и закрывается над ним;
#   6) продолжение  — две свечи с растущими максимумами и минимумами,
#                     цена всё ещё стоит у уровня входа.
#
# Ключевое свойство: сигнал СВЕЖИЙ — текущая цена отстоит от уровня входа
# на 0.22R, то есть находится внутри допуска stale-entry guard. Это и есть
# момент, в который сетап реально торгуется.
#
# Уровни не подгоняются под тесты — они выводятся из структуры:
# entry = закрытие ретеста, stop = минимум ретеста, цели = 2R и 3R.
# Час для SessionRule берётся из времени последней свечи, а не задаётся
# отдельной константой.
#
# build_context(stale_signal=True) даёт тот же сигнал, но с ушедшим вверх
# рынком (3.9R) — сценарий УСТАРЕВШЕГО сигнала, который обязан давать
# NO_TRADE.
# ---------------------------------------------------------------------------

FIVE_MINUTES = timedelta(minutes=5)
CANDLE_COUNT = 250

NEW_YORK = ZoneInfo("America/New_York")

# Сессия ORB, как её определяет vlad_orb.
SESSION_DATE = datetime(2026, 7, 28)
SESSION_OPEN = (9, 30)
RANGE_END = (10, 30)

# Последняя свеча ряда — внутри торгового окна 10:30-12:00.
LAST_CANDLE_AT = (11, 10)

# Границы опорного диапазона.
ORB_LOW = 99.0
ORB_HIGH = 101.0

# Геометрия ретеста. Значения выражены через границу диапазона, а не
# подобраны под тесты: откат уходит под orb_high, закрытие остаётся над ним.
RETEST_DEPTH = 0.8            # минимум ретеста = ORB_HIGH - 0.8
RETEST_CLOSE_ABOVE = 0.10     # закрытие ретеста = ORB_HIGH + 0.10

# Сколько свечей идёт ПОСЛЕ ретеста.
#
# Ретест намеренно не делается последней свечой. Свеча ретеста по
# определению обновляет минимум вниз, поэтому на ней MarketStructure
# (api/market_structure.py сравнивает две последние свечи) не может
# показать UPTREND. Реальная сделка берётся не на самом откате, а когда
# цена подтверждает продолжение и всё ещё стоит у уровня входа — именно
# это и воспроизводится: две свечи с растущими максимумами и минимумами,
# остающиеся в пределах допуска stale-entry guard.
CONTINUATION_CANDLES = 2


def _new_york(hour: int, minute: int) -> datetime:
    return datetime(
        SESSION_DATE.year,
        SESSION_DATE.month,
        SESSION_DATE.day,
        hour,
        minute,
        tzinfo=NEW_YORK,
    )


def _candle_open_times() -> list[datetime]:
    """
    Сетка времён открытия так, чтобы ПОСЛЕДНЯЯ свеча приходилась на момент
    ретеста. Смещение считается через zoneinfo, а не зашитым сдвигом UTC,
    поэтому сетка остаётся корректной и при смене перехода на летнее время.
    """
    last_open = _new_york(*LAST_CANDLE_AT)

    return [
        last_open - FIVE_MINUTES * (CANDLE_COUNT - 1 - index)
        for index in range(CANDLE_COUNT)
    ]


def _phase_of(candle_time: datetime) -> str:
    session_open = _new_york(*SESSION_OPEN)
    range_end = _new_york(*RANGE_END)

    if candle_time < session_open:
        return "BACKGROUND"

    if candle_time < range_end:
        return "RANGE"

    return "TRADE_WINDOW"


def build_synthetic_market(stale_signal: bool = False) -> dict:
    """
    Строит OHLC-ряды описанного сетапа.

    stale_signal=True добавляет после ретеста продолжение роста. Сигнал
    остаётся тем же (пробой и ретест уже произошли), но рынок уходит от
    уровня входа — это моделирует УСТАРЕВШИЙ сигнал, который торговать
    нельзя.
    """
    open_times = _candle_open_times()

    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []

    range_span = ORB_HIGH - ORB_LOW
    range_middle = (ORB_HIGH + ORB_LOW) / 2.0

    trade_window_index = 0

    for index, candle_time in enumerate(open_times):
        phase = _phase_of(candle_time)

        if phase == "BACKGROUND":
            # Умеренный многочасовой рост, подходящий к диапазону снизу.
            # Колебание нужно, чтобы структура не была искусственно
            # гладкой и индикаторы видели нормальный рынок.
            progress = index / max(1, CANDLE_COUNT - 1)
            close = (
                range_middle
                - 6.0 * (1.0 - progress)
                + 0.45 * math.sin(index * 0.35)
            )
            half_range = 0.30

        elif phase == "RANGE":
            # Колебание внутри коридора: касаемся обеих границ, чтобы
            # orb_high/orb_low определились именно как заданные уровни.
            oscillation = math.sin(index * 1.1)
            close = range_middle + 0.35 * range_span * oscillation
            half_range = 0.5 * range_span * 0.9

        else:
            retest_index = CANDLE_COUNT - 1 - CONTINUATION_CANDLES

            if trade_window_index == 0:
                # Пробой: закрытие уверенно выше границы диапазона.
                close = ORB_HIGH + 0.8
                half_range = 0.35
            elif index < retest_index:
                # Удержание выше границы. Минимумы обязаны оставаться выше
                # orb_high + допуск, иначе ретест нашёлся бы раньше и
                # сигнал перестал бы быть свежим.
                close = ORB_HIGH + 0.9 + 0.25 * math.sin(index * 0.9)
                half_range = 0.30
            elif index == retest_index:
                close = ORB_HIGH + RETEST_CLOSE_ABOVE
                half_range = 0.0
            else:
                # Подтверждение продолжения: каждая свеча выше предыдущей,
                # но цена ещё стоит у уровня входа.
                step = index - retest_index
                close = ORB_HIGH + RETEST_CLOSE_ABOVE + 0.10 * step
                half_range = 0.0

            trade_window_index += 1

        previous_close = closes[-1] if closes else close
        candle_open = previous_close

        if phase == "TRADE_WINDOW" and index >= CANDLE_COUNT - 1 - CONTINUATION_CANDLES:
            retest_index = CANDLE_COUNT - 1 - CONTINUATION_CANDLES
            step = index - retest_index

            # Ретест: минимум уходит под границу диапазона — это и делает
            # свечу ретестом. Далее минимумы и максимумы последовательно
            # растут, формируя UPTREND по двум последним свечам.
            candle_low = ORB_HIGH - RETEST_DEPTH + 0.30 * step
            candle_high = max(candle_open, close) + 0.10 + 0.05 * step
        else:
            candle_high = max(candle_open, close) + half_range
            candle_low = min(candle_open, close) - half_range

        if phase == "RANGE":
            # Прижимаем экстремумы диапазона к заявленным границам.
            candle_high = min(candle_high, ORB_HIGH)
            candle_low = max(candle_low, ORB_LOW)

        opens.append(candle_open)
        highs.append(candle_high)
        lows.append(candle_low)
        closes.append(close)

    if stale_signal:
        # Рынок уходит вверх ПОСЛЕ ретеста: сигнал тот же, но цена входа
        # больше не достижима.
        drift_candles = 6
        last_close = closes[-1]

        for step in range(1, drift_candles + 1):
            close = last_close + 0.55 * step
            candle_open = closes[-1]

            opens.append(candle_open)
            closes.append(close)
            highs.append(max(candle_open, close) + 0.25)
            lows.append(min(candle_open, close) - 0.25)
            open_times.append(open_times[-1] + FIVE_MINUTES)

    return {
        "price": closes[-1],
        "interval": "5m",
        "open_times_ms": [
            int(candle_time.timestamp() * 1000)
            for candle_time in open_times
        ],
        "opens": opens,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volume": 5000,
    }


def build_context(stale_signal: bool = False) -> MarketContext:
    context = MarketContext()

    context.exchange = "binance"
    context.symbol = "BTCUSDT"
    context.timeframe = "5m"

    context.market = build_synthetic_market(
        stale_signal=stale_signal,
    )

    context.portfolio = {
        "balance": 1000.0,
        # Единицы — ПРОЦЕНТЫ (api/risk_engine.py делит на 100),
        # то есть 0.1 == 0.1% капитала.
        "risk_percent": 0.1,
    }

    # Час берётся из ВРЕМЕНИ ПОСЛЕДНЕЙ СВЕЧИ, а не задаётся константой:
    # ряд моделирует конкретный момент внутри торгового окна ORB, и
    # SessionRule должна видеть тот же момент. Прежде здесь стояло
    # фиксированное 12, никак не связанное с временами свечей.
    last_open_ms = context.market["open_times_ms"][-1]
    last_open_utc = datetime.fromtimestamp(
        last_open_ms / 1000,
        tz=timezone.utc,
    )

    # Безопасная подмена времени разрешена только для DRY_RUN.
    context.execution = {
        "runtime": {
            "mode": "DRY_RUN",
            "utc_hour_override": last_open_utc.hour,
            "real_orders_enabled": False,
        },
    }

    return context


def main() -> None:
    print("=" * 60)
    print("TRADING CORE V2 - SAFE DRY RUN")
    print("REAL ORDERS: DISABLED")
    print("=" * 60)

    engine = Bootstrap.build()
    context = build_context()

    result = engine.execute(context)

    report = {
        "mode": "DRY_RUN",
        "real_order_sent": False,
        "runtime": result.execution.get("runtime"),
        "exchange": result.exchange,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "market_price": result.market.get("price"),
        "indicators": result.indicators,
        "strategy": result.strategy,
        "risk": result.risk,
        "trade_plan": result.execution.get("trade_plan"),
        "decision": result.decision,
        "audit": result.audit,
    }

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    print("=" * 60)
    print("DRY RUN COMPLETED")
    print("NO REAL ORDER WAS SENT")
    print("=" * 60)


if __name__ == "__main__":
    main()
