"""
Валидация и очистка свечей на границе с биржей.

Критично: Binance и Bybit возвращают последней ещё не закрытую
(текущую формирующуюся) свечу. Использование её high/low/close как
финальных значений даёт repainting и look-ahead-подобное поведение —
сигнал может "появиться" и через секунду исчезнуть, так как цена ещё
двигается. ORB_BASELINE.md явно требует входа только после закрытия
свечи. Эта проверка подтверждена эмпирически на реальных данных обеих
бирж перед фиксом.
"""


class StaleMarketDataError(Exception):
    """Полученные от биржи свечи не прошли базовую валидацию."""


INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

REQUIRED_KEYS = ("timestamps", "opens", "highs", "lows", "closes", "volumes")


def interval_to_ms(interval: str) -> int:

    if interval not in INTERVAL_MS:
        raise StaleMarketDataError(f"Неизвестный таймфрейм: {interval}")

    return INTERVAL_MS[interval]


def drop_unclosed_candle(candles: dict, interval: str, now_ms: int) -> dict:
    """
    Отбрасывает последнюю свечу, если её расчётное время закрытия
    ещё не наступило (то есть свеча всё ещё формируется).
    """

    timestamps = candles["timestamps"]

    if not timestamps:
        return candles

    interval_ms = interval_to_ms(interval)
    last_open = timestamps[-1]
    last_close_time = last_open + interval_ms

    if last_close_time > now_ms:
        return {key: candles[key][:-1] for key in REQUIRED_KEYS}

    return candles


def validate_candles(candles: dict, interval: str) -> None:
    """
    Базовая проверка целостности данных. Бросает StaleMarketDataError
    при обнаружении дублей, немонотонных timestamp, пропусков или
    некорректных (<=0 / NaN) цен и объёмов. Вызывающий код обязан
    перехватывать это исключение и безопасно возвращать NO_TRADE —
    молчаливое продолжение с повреждёнными данными запрещено.
    """

    for key in REQUIRED_KEYS:
        if key not in candles:
            raise StaleMarketDataError(f"В данных отсутствует поле: {key}")

    timestamps = candles["timestamps"]

    if len(timestamps) == 0:
        raise StaleMarketDataError("Нет ни одной закрытой свечи.")

    lengths = {key: len(candles[key]) for key in REQUIRED_KEYS}
    if len(set(lengths.values())) != 1:
        raise StaleMarketDataError(f"Несогласованная длина полей свечей: {lengths}")

    interval_ms = interval_to_ms(interval)

    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]

        if gap <= 0:
            raise StaleMarketDataError(
                f"Свечи не в хронологическом порядке или дублируются "
                f"на индексе {i} (timestamps[{i-1}]={timestamps[i-1]}, "
                f"timestamps[{i}]={timestamps[i]})."
            )

        if gap != interval_ms:
            raise StaleMarketDataError(
                f"Пропуск свечи между индексами {i-1} и {i}: "
                f"ожидался шаг {interval_ms} мс, получено {gap} мс."
            )

    for key in ("opens", "highs", "lows", "closes"):
        for value in candles[key]:
            if value != value:  # NaN
                raise StaleMarketDataError(f"NaN значение в поле {key}.")
            if value <= 0:
                raise StaleMarketDataError(f"Некорректная (<=0) цена в поле {key}: {value}")

    for value in candles["volumes"]:
        if value != value or value < 0:
            raise StaleMarketDataError(f"Некорректный объём: {value}")

    highs = candles["highs"]
    lows = candles["lows"]
    opens = candles["opens"]
    closes = candles["closes"]

    for i in range(len(timestamps)):
        candle_high = highs[i]
        candle_low = lows[i]

        if candle_low > candle_high:
            raise StaleMarketDataError(f"low > high на индексе {i}.")

        if not (candle_low <= opens[i] <= candle_high):
            raise StaleMarketDataError(f"open вне диапазона [low, high] на индексе {i}.")

        if not (candle_low <= closes[i] <= candle_high):
            raise StaleMarketDataError(f"close вне диапазона [low, high] на индексе {i}.")
