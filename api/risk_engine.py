import math


class RiskEngine:

    @staticmethod
    def calculate(
        balance: float,
        risk_percent: float,
        price: float,
        atr: float,
    ):
        """
        NB: atr здесь используется как stop_distance только для
        стратегий, где стоп буквально равен ATR (см. TradePlan).
        Для ORB (стоп = граница диапазона ± 0.2*ATR) вызывающий код
        обязан передавать сюда реальную дистанцию до стопа, а не
        сырой ATR — иначе размер позиции не будет соответствовать
        заявленному риску на сделку.
        """

        for name, value in (
            ("balance", balance),
            ("risk_percent", risk_percent),
            ("price", price),
            ("atr", atr),
        ):
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                return {
                    "allowed": False,
                    "reason": f"Некорректный тип параметра {name}.",
                }
            if math.isnan(value):
                return {
                    "allowed": False,
                    "reason": f"NaN значение параметра {name}.",
                }
            if math.isinf(value):
                return {
                    "allowed": False,
                    "reason": f"Бесконечное значение параметра {name}.",
                }

        if balance <= 0:
            return {
                "allowed": False,
                "reason": "Баланс должен быть положительным.",
            }

        if risk_percent <= 0:
            return {
                "allowed": False,
                "reason": "risk_percent должен быть положительным.",
            }

        if price <= 0:
            return {
                "allowed": False,
                "reason": "Цена должна быть положительной.",
            }

        if atr <= 0:
            return {
                "allowed": False,
                "reason": "ATR is zero",
            }

        risk_amount = balance * (risk_percent / 100)

        stop_distance = atr

        position_size = risk_amount / stop_distance

        if position_size <= 0:
            return {
                "allowed": False,
                "reason": "Рассчитанный размер позиции не положителен.",
            }

        return {
            "allowed": True,
            "risk_amount": round(risk_amount, 2),
            "position_size": round(position_size, 6),
            "stop_distance": round(stop_distance, 2),
        }


class DailyRiskGuard:
    """
    Дневной лимит количества сделок и суммарного риска.

    Это состояние в памяти процесса (сбрасывается по UTC-дате и при
    рестарте). risk_committed_today — это сумма ПЛАНИРУЕМОГО риска на
    открытые сделки, а не реализованный PnL: система пока не считает
    фактический результат закрытых сделок, поэтому это осознанно
    консервативная прокси-метрика, а не точный дневной P&L.
    """

    _date = None
    _trades_today = 0
    _risk_committed_today = 0.0

    @classmethod
    def _reset_if_new_day(cls):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date()

        if cls._date != today:
            cls._date = today
            cls._trades_today = 0
            cls._risk_committed_today = 0.0

    @classmethod
    def check(cls, balance: float, risk_amount: float, max_trades: int, max_risk_percent: float):
        cls._reset_if_new_day()

        if cls._trades_today >= max_trades:
            return {
                "allowed": False,
                "reason": f"Достигнут дневной лимит сделок ({max_trades}).",
            }

        max_daily_risk = balance * (max_risk_percent / 100)

        if cls._risk_committed_today + risk_amount > max_daily_risk:
            return {
                "allowed": False,
                "reason": "Достигнут дневной лимит риска.",
            }

        return {"allowed": True, "reason": None}

    @classmethod
    def register_trade(cls, risk_amount: float):
        cls._reset_if_new_day()
        cls._trades_today += 1
        cls._risk_committed_today += risk_amount

    @classmethod
    def reset(cls):
        """Только для тестов."""
        cls._date = None
        cls._trades_today = 0
        cls._risk_committed_today = 0.0


class LossStreakGuard:
    """
    Отслеживает серию убытков, просадку и cooldown после убытка.

    Все лимиты fail-closed: при достижении любого из них новые входы
    блокируются до явного сброса оператором (или до следующего дня для
    дневных лимитов).
    """

    _consecutive_losses = 0
    _last_loss_at = None
    _peak_equity = None
    _session_trades = {}   # session_key -> count

    @classmethod
    def register_result(cls, net_pnl: float, equity: float = None, session_key=None):
        import time

        if net_pnl < 0:
            cls._consecutive_losses += 1
            cls._last_loss_at = time.time()
        elif net_pnl > 0:
            cls._consecutive_losses = 0

        if equity is not None:
            if cls._peak_equity is None or equity > cls._peak_equity:
                cls._peak_equity = equity

        if session_key is not None:
            cls._session_trades[session_key] = cls._session_trades.get(session_key, 0) + 1

    @classmethod
    def check(cls, equity: float = None, session_key=None,
              max_consecutive_losses: int = 3, max_drawdown_percent: float = 5.0,
              cooldown_seconds: float = 3600, max_trades_per_session: int = 1):
        import time

        if cls._consecutive_losses >= max_consecutive_losses:
            return {
                "allowed": False,
                "reason": f"Достигнут лимит серии убытков ({cls._consecutive_losses}). Требуется вмешательство оператора.",
            }

        if cls._last_loss_at is not None:
            elapsed = time.time() - cls._last_loss_at
            if elapsed < cooldown_seconds:
                remaining = int(cooldown_seconds - elapsed)
                return {
                    "allowed": False,
                    "reason": f"Пауза после убыточной сделки: осталось {remaining} с.",
                }

        if equity is not None and cls._peak_equity:
            drawdown_percent = (cls._peak_equity - equity) / cls._peak_equity * 100
            if drawdown_percent >= max_drawdown_percent:
                return {
                    "allowed": False,
                    "reason": f"Просадка {drawdown_percent:.2f}% достигла лимита {max_drawdown_percent}%.",
                }

        if session_key is not None:
            traded = cls._session_trades.get(session_key, 0)
            if traded >= max_trades_per_session:
                return {
                    "allowed": False,
                    "reason": f"В этой сессии уже совершено {traded} сделок (лимит {max_trades_per_session}).",
                }

        return {"allowed": True, "reason": None}

    @classmethod
    def reset(cls):
        """Только для тестов и явного сброса оператором."""
        cls._consecutive_losses = 0
        cls._last_loss_at = None
        cls._peak_equity = None
        cls._session_trades = {}