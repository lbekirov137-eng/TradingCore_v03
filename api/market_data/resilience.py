"""
Устойчивость REST-провайдеров рыночных данных: переподключение с
экспоненциальной паузой, обработка rate-limit (HTTP 429) и обнаружение
рассинхронизации часов (clock skew) между локальным временем и временем
биржи.

Missing-candle и duplicate-candle detection уже реализованы в
api/market_data/candle_utils.py (validate_candles) — здесь не дублируются.
"""

import time

import requests


class RateLimitExceededError(Exception):
    pass


def retry_with_backoff(fn, max_retries: int = 3, base_delay: float = 1.0,
                        sleep_fn=time.sleep, retryable_status_codes=(429, 500, 502, 503, 504)):
    """
    Вызывает fn() с повторными попытками при транзиентных сетевых ошибках
    (timeout/connection error) и HTTP-статусах из retryable_status_codes
    (в т.ч. 429 rate limit — с экспоненциальной паузой).

    НЕ повторяет запрос при постоянных ошибках (4xx кроме 429) — они
    сразу пробрасываются, чтобы не маскировать некорректный запрос
    бесконечными ретраями.
    """

    last_error = None

    for attempt in range(max_retries):
        try:
            return fn()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            last_error = error

            if status not in retryable_status_codes:
                raise

            if attempt == max_retries - 1:
                if status == 429:
                    raise RateLimitExceededError(
                        f"Rate limit exceeded after {max_retries} attempts."
                    ) from error
                raise

            sleep_fn(base_delay * (2 ** attempt))

        except (requests.ConnectionError, requests.Timeout) as error:
            last_error = error

            if attempt == max_retries - 1:
                raise

            sleep_fn(base_delay * (2 ** attempt))

    raise last_error


class ClockSkewChecker:
    """
    Сравнивает локальное время с временем сервера биржи. Значительное
    расхождение — признак некорректных часов на хосте (может приводить
    к ложному определению устаревших данных или к отклонению подписанных
    запросов биржей).
    """

    @staticmethod
    def check(server_time_ms: int, local_time_ms: int = None, max_skew_seconds: float = 5.0) -> dict:

        local_time_ms = local_time_ms if local_time_ms is not None else int(time.time() * 1000)

        skew_seconds = (local_time_ms - server_time_ms) / 1000

        return {
            "skewed": abs(skew_seconds) > max_skew_seconds,
            "skew_seconds": round(skew_seconds, 3),
            "max_skew_seconds": max_skew_seconds,
            "server_time_ms": server_time_ms,
            "local_time_ms": local_time_ms,
        }
