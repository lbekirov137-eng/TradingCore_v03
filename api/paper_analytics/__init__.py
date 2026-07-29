"""
Наблюдение и оценка результатов paper-контура.

Пакет НЕ участвует в принятии торговых решений. Он только читает то, что
цикл уже записал, и сводит это в пригодный для проверки вид. Ни одна
функция здесь не выставляет ордера, не меняет риск, сигналы и правила
входа и не может быть вызвана из торгового пути так, чтобы повлиять на
решение.
"""

from api.paper_analytics.observation import (
    build_observation,
)
from api.paper_analytics.report import (
    INSUFFICIENT_SAMPLE,
    MIN_CLOSED_TRADES_FOR_PRELIMINARY_READ,
    SAFE,
    STOP,
    WARNING,
    build_report,
    journal_path,
    load_records,
    render_report_text,
)

__all__ = [
    "build_observation",
    "build_report",
    "journal_path",
    "load_records",
    "render_report_text",
    "INSUFFICIENT_SAMPLE",
    "MIN_CLOSED_TRADES_FOR_PRELIMINARY_READ",
    "SAFE",
    "WARNING",
    "STOP",
]
