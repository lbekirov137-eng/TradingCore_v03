"""
Именованные защитные механизмы (guards), каждый — отдельный, тестируемый
класс с единственной ответственностью. Раньше несколько из них были
объединены в один класс LossStreakGuard; разделены по явному требованию,
чтобы каждая заблокированная сделка возвращала машинно-читаемую причину
от конкретного guard'а, а не от неопределённой смеси условий.

Все guard'ы fail-closed: при достижении лимита — блокировка, до явного
сброса (оператором или наступлением нового дня для дневных лимитов).
Всё состояние — в памяти процесса (см. известное ограничение:
не multi-process-safe, ISSUES.md H-5).
"""

import time
from datetime import datetime, timezone


class LossStreakGuard:
    """Блокирует после N подряд идущих убыточных сделок."""

    _consecutive_losses = 0

    @classmethod
    def register_result(cls, net_pnl: float):
        if net_pnl < 0:
            cls._consecutive_losses += 1
        elif net_pnl > 0:
            cls._consecutive_losses = 0

    @classmethod
    def check(cls, max_consecutive_losses: int = 3):
        if cls._consecutive_losses >= max_consecutive_losses:
            return {
                "allowed": False,
                "guard": "LossStreakGuard",
                "reason": f"Серия убытков достигла {cls._consecutive_losses} "
                          f"(лимит {max_consecutive_losses}). Требуется решение оператора.",
            }
        return {"allowed": True, "guard": "LossStreakGuard", "reason": None}

    @classmethod
    def reset(cls):
        cls._consecutive_losses = 0


class CooldownAfterLossGuard:
    """Пауза перед новым входом после последней убыточной сделки."""

    _last_loss_at = None

    @classmethod
    def register_result(cls, net_pnl: float):
        if net_pnl < 0:
            cls._last_loss_at = time.time()

    @classmethod
    def check(cls, cooldown_seconds: float = 3600):
        if cls._last_loss_at is None:
            return {"allowed": True, "guard": "CooldownAfterLossGuard", "reason": None}

        elapsed = time.time() - cls._last_loss_at

        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            return {
                "allowed": False,
                "guard": "CooldownAfterLossGuard",
                "reason": f"Пауза после убыточной сделки: осталось {remaining} с.",
            }

        return {"allowed": True, "guard": "CooldownAfterLossGuard", "reason": None}

    @classmethod
    def reset(cls):
        cls._last_loss_at = None


class MaxDrawdownGuard:
    """Останавливает торговлю при просадке от пикового equity выше лимита."""

    _peak_equity = None

    @classmethod
    def register_equity(cls, equity: float):
        if equity is None:
            return
        if cls._peak_equity is None or equity > cls._peak_equity:
            cls._peak_equity = equity

    @classmethod
    def check(cls, equity: float, max_drawdown_percent: float = 5.0):
        if equity is None or cls._peak_equity is None or cls._peak_equity <= 0:
            return {"allowed": True, "guard": "MaxDrawdownGuard", "reason": None}

        drawdown_percent = (cls._peak_equity - equity) / cls._peak_equity * 100

        if drawdown_percent >= max_drawdown_percent:
            return {
                "allowed": False,
                "guard": "MaxDrawdownGuard",
                "reason": f"Просадка {drawdown_percent:.2f}% достигла лимита {max_drawdown_percent}%.",
            }

        return {"allowed": True, "guard": "MaxDrawdownGuard", "reason": None}

    @classmethod
    def reset(cls):
        cls._peak_equity = None


class MaxTradesPerSessionGuard:
    """Ограничивает число сделок в рамках одной торговой сессии (по session_key)."""

    _session_trades = {}

    @classmethod
    def register_trade(cls, session_key):
        if session_key is None:
            return
        cls._session_trades[session_key] = cls._session_trades.get(session_key, 0) + 1

    @classmethod
    def check(cls, session_key, max_trades_per_session: int = 1):
        if session_key is None:
            return {"allowed": True, "guard": "MaxTradesPerSessionGuard", "reason": None}

        traded = cls._session_trades.get(session_key, 0)

        if traded >= max_trades_per_session:
            return {
                "allowed": False,
                "guard": "MaxTradesPerSessionGuard",
                "reason": f"В этой сессии уже {traded} сделок (лимит {max_trades_per_session}).",
            }

        return {"allowed": True, "guard": "MaxTradesPerSessionGuard", "reason": None}

    @classmethod
    def reset(cls):
        cls._session_trades = {}


class DailyLossGuard:
    """
    Лимит РЕАЛИЗОВАННОГО убытка за календарные сутки (UTC) — в отличие от
    DailyRiskGuard (api/risk_engine.py), который лимитирует ПЛАНИРУЕМЫЙ
    риск в момент открытия. Этот guard считает фактический результат
    закрытых сделок, поэтому единственный источник данных для него —
    net_pnl закрытой сделки, а не размер позиции при входе.
    """

    _date = None
    _realized_pnl_today = 0.0

    @classmethod
    def _reset_if_new_day(cls):
        today = datetime.now(timezone.utc).date()
        if cls._date != today:
            cls._date = today
            cls._realized_pnl_today = 0.0

    @classmethod
    def register_result(cls, net_pnl: float):
        cls._reset_if_new_day()
        cls._realized_pnl_today += net_pnl

    @classmethod
    def check(cls, balance: float, max_daily_loss_percent: float = 2.0):
        cls._reset_if_new_day()

        if cls._realized_pnl_today >= 0:
            return {"allowed": True, "guard": "DailyLossGuard", "reason": None}

        max_daily_loss = balance * (max_daily_loss_percent / 100)
        realized_loss = abs(cls._realized_pnl_today)

        if realized_loss >= max_daily_loss:
            return {
                "allowed": False,
                "guard": "DailyLossGuard",
                "reason": f"Реализованный убыток за сутки {realized_loss:.2f} "
                          f"достиг лимита {max_daily_loss:.2f} ({max_daily_loss_percent}%).",
            }

        return {"allowed": True, "guard": "DailyLossGuard", "reason": None}

    @classmethod
    def reset(cls):
        cls._date = None
        cls._realized_pnl_today = 0.0


class MaxOpenPositionsGuard:
    """
    Оборачивает PositionManager для единообразного, машинно-читаемого
    отчёта о причине блокировки. Текущая архитектура поддерживает РОВНО
    одну открытую позицию (см. PositionManager) — это не настраиваемый
    параметр, а структурное свойство, задокументированное здесь явно.
    """

    @staticmethod
    def check():
        from api.position_manager.position_manager import PositionManager

        if PositionManager.has_open_position():
            return {
                "allowed": False,
                "guard": "MaxOpenPositionsGuard",
                "reason": "Достигнут лимит одновременно открытых позиций (1).",
            }

        return {"allowed": True, "guard": "MaxOpenPositionsGuard", "reason": None}


def reset_all():
    """Только для тестов: сбрасывает состояние всех guard'ов сразу."""
    LossStreakGuard.reset()
    CooldownAfterLossGuard.reset()
    MaxDrawdownGuard.reset()
    MaxTradesPerSessionGuard.reset()
    DailyLossGuard.reset()
