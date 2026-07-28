from dataclasses import asdict

from api.contracts.context import MarketContext
from api.decision_engine.rules.risk_rule import RiskRule
from api.position_manager.position_manager import PositionManager
from api.risk_engine import DailyRiskGuard
from api.execution.position_sizing import PositionSizer
from api.execution.kill_switch import KillSwitch
from api.risk.guards import (
    LossStreakGuard,
    CooldownAfterLossGuard,
    MaxDrawdownGuard,
    MaxTradesPerSessionGuard,
    DailyLossGuard,
    MaxOpenPositionsGuard,
)

from config.settings import (
    DEFAULT_BALANCE,
    DEFAULT_RISK_PERCENT,
    MIN_RISK_REWARD,
    MAX_DAILY_TRADES,
    MAX_DAILY_RISK_PERCENT,
    DEFAULT_FEE_RATE,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_TICK_SIZE,
    DEFAULT_LOT_SIZE,
    DEFAULT_MIN_NOTIONAL,
    MAX_POSITION_PERCENT_OF_BALANCE,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DRAWDOWN_PERCENT,
    MAX_DAILY_LOSS_PERCENT,
    COOLDOWN_AFTER_LOSS_SECONDS,
    MAX_TRADES_PER_SESSION,
)


kill_switch = KillSwitch()


class DecisionEngine:

    NAME = "Decision Engine"

    VERSION = "1.0.0"

    @staticmethod
    def process(context: MarketContext) -> MarketContext:
        """Устаревший путь для старого MarketPipeline. Всегда NO_TRADE — не реализован."""

        context.decision = {
            "decision": "NO_TRADE",
            "reason": "Decision Engine пока не реализован"
        }

        return context

    @staticmethod
    def decide(context):
        """
        Реальный путь принятия решения для paper/live тика (Scheduler/Workflow).

        По умолчанию — NO_TRADE. Сделка одобряется только если пройдены ВСЕ
        проверки: сигнал стратегии подтверждён, нет открытой позиции, торговый
        план полон и согласован, R:R не хуже минимального, риск-лимиты и
        дневные лимиты не превышены, ордер не является дублем/повтором сессии.
        """

        signals = getattr(context, "strategy_signals", None) or []
        approved = [s for s in signals if s and s.get("approved")]

        exchange = getattr(context, "exchange", None)
        symbol = getattr(context, "symbol", None)

        if kill_switch.is_engaged():
            return DecisionEngine._no_trade(
                context, exchange, symbol,
                f"Kill switch активирован: {kill_switch.status()['reason']}",
                None,
            )

        if not approved:
            reason = signals[0]["reason"] if signals else "Нет сигналов стратегии."
            strategy_name = signals[0].get("strategy") if signals else None
            return DecisionEngine._no_trade(context, exchange, symbol, reason, strategy_name)

        signal = approved[0]
        strategy_name = signal.get("strategy")

        open_position_check = MaxOpenPositionsGuard.check()
        if not open_position_check["allowed"]:
            return DecisionEngine._no_trade(
                context, exchange, symbol, open_position_check["reason"], strategy_name
            )

        # Account-level guards: checked BEFORE parsing the trade plan, so a
        # paused account (losing streak, cooldown, drawdown, daily loss)
        # never depends on the specifics of the current signal.
        current_equity = DecisionEngine._current_equity(symbol=symbol)
        MaxDrawdownGuard.register_equity(current_equity)

        for guard_check in (
            LossStreakGuard.check(max_consecutive_losses=MAX_CONSECUTIVE_LOSSES),
            CooldownAfterLossGuard.check(cooldown_seconds=COOLDOWN_AFTER_LOSS_SECONDS),
            MaxDrawdownGuard.check(equity=current_equity, max_drawdown_percent=MAX_DRAWDOWN_PERCENT),
            DailyLossGuard.check(balance=DEFAULT_BALANCE, max_daily_loss_percent=MAX_DAILY_LOSS_PERCENT),
        ):
            if not guard_check["allowed"]:
                return DecisionEngine._no_trade(
                    context, exchange, symbol, guard_check["reason"], strategy_name
                )

        trade_plan = signal.get("trade_plan") or {}
        entry = trade_plan.get("entry")
        stop = trade_plan.get("stop_loss")
        take_profit = trade_plan.get("take_profit") or {}
        tp1 = take_profit.get("tp1")

        if entry is None or stop is None or tp1 is None or entry == stop:
            return DecisionEngine._no_trade(
                context, exchange, symbol,
                "Неполный или некорректный торговый план.", strategy_name,
            )

        risk_distance = abs(entry - stop)
        reward_distance = abs(tp1 - entry)
        rr_ratio = reward_distance / risk_distance if risk_distance else 0.0

        if rr_ratio < MIN_RISK_REWARD:
            return DecisionEngine._no_trade(
                context, exchange, symbol,
                f"R:R {rr_ratio:.2f} ниже минимального 1:{MIN_RISK_REWARD:.0f}.",
                strategy_name,
            )

        # Полный расчёт размера позиции: реальная дистанция до стопа
        # (entry-stop, а не сырой ATR — для ORB это разные величины,
        # т.к. стоп = граница диапазона ± 0.2*ATR), плюс комиссии,
        # проскальзывание, tick/lot size и minimum notional.
        #
        # available_balance пока равен balance (нет отдельного трекера
        # использованного баланса до появления PaperBroker/Phase 5) —
        # задокументированное упрощение, не ошибка.
        size_result = PositionSizer.calculate(
            balance=DEFAULT_BALANCE,
            available_balance=DEFAULT_BALANCE,
            risk_percent=DEFAULT_RISK_PERCENT,
            entry=entry,
            stop=stop,
            fee_rate=DEFAULT_FEE_RATE,
            slippage_bps=DEFAULT_SLIPPAGE_BPS,
            tick_size=DEFAULT_TICK_SIZE,
            lot_size=DEFAULT_LOT_SIZE,
            min_notional=DEFAULT_MIN_NOTIONAL,
            max_position_percent_of_balance=MAX_POSITION_PERCENT_OF_BALANCE,
        )

        risk = {
            "allowed": size_result.allowed,
            "reason": size_result.reason,
            "risk_amount": size_result.risk_amount,
            "position_size": size_result.quantity,
            "notional": size_result.notional,
            "fee_amount": size_result.fee_amount,
            "slippage_amount": size_result.slippage_amount,
            "stop_distance": size_result.effective_stop_distance,
        }

        context.risk = risk

        risk_check = RiskRule.evaluate(context)

        if not risk_check["passed"]:
            return DecisionEngine._no_trade(
                context, exchange, symbol, risk_check["reason"], strategy_name
            )

        daily_check = DailyRiskGuard.check(
            balance=DEFAULT_BALANCE,
            risk_amount=risk["risk_amount"],
            max_trades=MAX_DAILY_TRADES,
            max_risk_percent=MAX_DAILY_RISK_PERCENT,
        )

        if not daily_check["allowed"]:
            return DecisionEngine._no_trade(
                context, exchange, symbol, daily_check["reason"], strategy_name
            )

        metadata = signal.get("metadata") or {}
        opening_range = metadata.get("opening_range") or {}

        session_key = (
            exchange, symbol, strategy_name,
            opening_range.get("session"), opening_range.get("timestamp"),
        )

        if PositionManager.is_duplicate_session(session_key):
            return DecisionEngine._no_trade(
                context, exchange, symbol,
                "Эта сессия уже отторгована — повторный вход запрещён.",
                strategy_name,
            )

        session_count_check = MaxTradesPerSessionGuard.check(
            session_key, max_trades_per_session=MAX_TRADES_PER_SESSION
        )
        if not session_count_check["allowed"]:
            return DecisionEngine._no_trade(
                context, exchange, symbol, session_count_check["reason"], strategy_name
            )

        signature = (exchange, symbol, signal.get("direction"), entry, stop)

        if PositionManager.is_duplicate_signature(signature):
            return DecisionEngine._no_trade(
                context, exchange, symbol,
                "Повторная отправка идентичного ордера заблокирована.",
                strategy_name,
            )

        decision = {
            "decision": "TRADE",
            "reason": signal.get("reason", "Сигнал подтверждён."),
            "exchange": exchange,
            "symbol": symbol,
            "strategy": strategy_name,
            "direction": signal.get("direction"),
            "trade_plan": trade_plan,
            "confidence": signal.get("confidence"),
            "risk": risk,
            "risk_reward_ratio": round(rr_ratio, 2),
            "signature": signature,
            "session_key": session_key,
        }

        context.decision = decision

        return decision

    @staticmethod
    def _current_equity(symbol=None):
        """
        Equity по себестоимости: денежный баланс + стоимость открытой
        позиции ПО ЦЕНЕ ВХОДА (не по текущей рыночной цене).

        КРИТИЧНО #1: если считать equity как один только денежный баланс,
        само ОТКРЫТИЕ позиции (конвертация кэша в актив равной стоимости)
        выглядело бы как мгновенная просадка — подтверждённый баг, найден
        тестом test_h2 (после первой сделки MaxDrawdownGuard ложно сообщал
        "просадка 12.70%", хотя реального убытка не было).

        КРИТИЧНО #2: положение позиции запрашивается напрямую у БРОКЕРА
        (источник истины), а не у PositionManager — тест на восстановление
        после рестарта (test_h2) специально очищает PositionManager, чтобы
        смоделировать потерю локального состояния, пока у брокера позиция
        всё ещё существует. Если бы equity опирался на PositionManager,
        он был бы слеп именно в тот момент, когда сверка нужнее всего.

        Оценка по себестоимости, а не по рынку — задокументированное
        упрощение: полноценный mark-to-market потребовал бы live-цену на
        каждом шаге принятия решения, которая здесь не гарантированно
        доступна. Реальные убытки (после закрытия сделки) корректно
        уменьшают денежный баланс и поэтому корректно отражаются здесь.
        """
        try:
            from api.trade_engine import trade_engine as te

            balance = te.broker.get_balance()["balance"]

            if symbol is not None:
                broker_position = te.broker.get_position(symbol)
                balance += broker_position.get("qty", 0.0) * broker_position.get("avg_entry", 0.0)

            return balance
        except Exception:
            return None

    @staticmethod
    def _no_trade(context, exchange, symbol, reason, strategy=None):
        decision = {
            "decision": "NO_TRADE",
            "reason": reason,
            "exchange": exchange,
            "symbol": symbol,
            "strategy": strategy,
        }

        context.decision = decision

        return decision
