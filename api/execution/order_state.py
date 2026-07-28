"""
Состояния ордера и генерация детерминированного client_order_id.

client_order_id генерируется ДЕТЕРМИНИРОВАННО из логической сигнатуры
решения (биржа, инструмент, направление, вход, стоп, session_key), а НЕ
случайно. Это ключевое свойство идемпотентности: если исходный запрос
таймаутится и код повторяет попытку с теми же входными данными, он
обязан получить тот же самый client_order_id — тогда либо биржа сама
отклонит дубликат (idempotency на стороне биржи), либо наш reconciler
обнаружит, что ордер с этим ID уже существует, и не отправит второй.

Соответствует явному требованию: "Never resend an order merely because
the first request timed out" — id стабилен, а решение "отправлять ли
заново" принимает исключительно OrderReconciler после сверки с биржей.
"""

import hashlib
from enum import Enum


class OrderStatus(str, Enum):

    NEW = "NEW"                          # создан локально, ещё не отправлен
    SUBMITTED = "SUBMITTED"               # запрос отправлен, ответ не получен (таймаут/обрыв)
    ACKNOWLEDGED = "ACKNOWLEDGED"          # биржа подтвердила приём
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"                    # состояние неизвестно, требуется reconciliation
    FAILED_SAFELY = "FAILED_SAFELY"        # безопасно отклонён нашей же системой (не биржей)


# Состояния, при которых НЕЛЬЗЯ повторно отправлять тот же ордер без
# явного подтверждения от биржи, что оригинал не был принят.
NON_RETRYABLE_WITHOUT_RECONCILIATION = {
    OrderStatus.SUBMITTED,
    OrderStatus.ACKNOWLEDGED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED,
    OrderStatus.UNKNOWN,
}


def generate_client_order_id(exchange: str, symbol: str, direction: str,
                              entry: float, stop: float, session_key) -> str:

    raw = f"{exchange}|{symbol}|{direction}|{entry}|{stop}|{session_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    return f"tc-{digest}"


def generate_exit_client_order_id(entry_client_order_id: str, exit_reason: str, attempt: int = 0) -> str:
    """
    Детерминированный ID для ордера ЗАКРЫТИЯ, производный от ID входного
    ордера — чтобы повторная попытка закрытия той же позиции по той же
    причине (например, после таймаута reconciliation) переиспользовала
    тот же client_order_id, а не создавала второй ордер на закрытие.
    """
    raw = f"exit|{entry_client_order_id}|{exit_reason}|{attempt}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"tc-exit-{digest}"
