"""
Операторские отчёты: PnL, открытые позиции, риск-статус.

Все цифры берутся из фактического состояния paper-брокера и
риск-движка — ничего не оценивается «на глаз» и не додумывается.
"""

from api.position_manager.position_manager import PositionManager
from api.risk_engine import DailyRiskGuard
from api.decision_engine.decision_engine import kill_switch

from config.settings import (
    DEFAULT_BALANCE,
    DEFAULT_RISK_PERCENT,
    MIN_RISK_REWARD,
    MAX_DAILY_TRADES,
    MAX_DAILY_RISK_PERCENT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DRAWDOWN_PERCENT,
)


def pnl_report() -> dict:
    from api.trade_engine import trade_engine as te

    balance = te.broker.get_balance()

    trades = te.journal.trades
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    opened = [t for t in trades if t.get("status") == "OPENED"]
    failed = [t for t in trades if t.get("status") == "FAILED_SAFELY"]

    return {
        "starting_balance": DEFAULT_BALANCE,
        "current_balance": balance["balance"],
        "available_balance": balance["available_balance"],
        "realized_pnl": balance["realized_pnl"],
        "return_percent": round(
            (balance["balance"] + balance["realized_pnl"] - DEFAULT_BALANCE) / DEFAULT_BALANCE * 100, 4
        ) if DEFAULT_BALANCE else 0.0,
        "trades_opened": len(opened),
        "trades_closed": len(closed),
        "trades_failed_safely": len(failed),
        "note": (
            "realized_pnl считается только по фактически закрытым paper-сделкам. "
            "Незакрытые позиции не учитываются как прибыль."
        ),
    }


def open_position_report() -> dict:
    from api.trade_engine import trade_engine as te

    if not PositionManager.has_open_position():
        return {"has_open_position": False, "position": None}

    position = PositionManager.current_position()
    symbol = position.get("symbol")

    broker_position = te.broker.get_position(symbol)

    return {
        "has_open_position": True,
        "position": position,
        "broker_view": broker_position,
        "in_sync": abs(broker_position.get("qty", 0) - (position.get("qty") or 0)) < 1e-9,
    }


def risk_report() -> dict:
    DailyRiskGuard._reset_if_new_day()

    max_daily_risk = DEFAULT_BALANCE * (MAX_DAILY_RISK_PERCENT / 100)

    return {
        "risk_percent_per_trade": DEFAULT_RISK_PERCENT,
        "min_risk_reward": MIN_RISK_REWARD,
        "max_daily_trades": MAX_DAILY_TRADES,
        "trades_today": DailyRiskGuard._trades_today,
        "trades_remaining_today": max(0, MAX_DAILY_TRADES - DailyRiskGuard._trades_today),
        "max_daily_risk_percent": MAX_DAILY_RISK_PERCENT,
        "max_daily_risk_amount": round(max_daily_risk, 8),
        "risk_committed_today": round(DailyRiskGuard._risk_committed_today, 8),
        "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
        "max_drawdown_percent": MAX_DRAWDOWN_PERCENT,
        "kill_switch_engaged": kill_switch.is_engaged(),
        "leverage": 1,
        "averaging_down_enabled": False,
        "note": (
            "risk_committed_today — это ПЛАНИРУЕМЫЙ риск открытых сделок, "
            "а не реализованный убыток."
        ),
    }
