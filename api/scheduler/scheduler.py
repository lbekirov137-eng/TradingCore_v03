from api.data_engine import DataEngine
from api.strategy_engine.strategy_engine import StrategyEngine
from api.decision_engine.decision_engine import DecisionEngine

from api.ema import EMAEngine
from api.rsi import RSIEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure

from api.market_data.candle_utils import StaleMarketDataError


class Scheduler:

    @staticmethod
    def tick(context):
        """
        Один цикл paper/live тика. Любая ошибка биржи, сети или данных
        обязана заканчиваться безопасным NO_TRADE, а не необработанным
        исключением — иначе вызывающий код (API/n8n/Telegram) получит
        падение вместо предсказуемого ответа.

        Обновляет health-трекер (heartbeat + timestamp последней свечи)
        независимо от того, кто вызвал tick — ручной /paper/tick или
        автоматический SchedulerLoop, — чтобы /health отражал ЛЮБОЙ
        реально выполненный цикл, а не только автоматические.
        """

        from api.observability.states import health

        try:
            market = DataEngine.load(
                exchange=context.exchange,
                symbol=context.symbol,
                interval=context.interval,
                limit=context.limit,
            )

            context.market = market

            if len(market.timestamps) > 0:
                health.record_market_data(market.timestamps[-1])
            else:
                health.heartbeat()

            # Для детерминированного replay «сейчас» задаётся временем
            # последней свечи; в живом режиме now_ms остаётся None и
            # используются реальные часы.
            if getattr(context, "now_ms", None) is None and getattr(context, "replay_mode", False):
                context.now_ms = market.timestamps[-1]

            Scheduler._compute_indicators(context)

            signals = StrategyEngine.generate(context)
            context.strategy_signals = signals

            decision = DecisionEngine.decide(context)

            return decision

        except StaleMarketDataError as error:
            health.heartbeat()
            return Scheduler._safe_stop(context, f"Данные биржи не прошли проверку: {error}")

        except Exception as error:
            # Намеренно широкий except: это граница безопасности между
            # внешним миром (биржа/сеть/время) и торговым решением.
            # Любая непредвиденная ошибка обязана давать NO_TRADE, а не
            # приводить к падению процесса или неопределённому состоянию.
            health.heartbeat()
            return Scheduler._safe_stop(
                context, f"Безопасная остановка: {type(error).__name__}: {error}"
            )

    @staticmethod
    def _compute_indicators(context):

        market = context.market

        context.indicators["ema"] = EMAEngine.calculate_all(market.closes)
        context.indicators["rsi"] = RSIEngine.calculate(market.closes)
        context.indicators["atr"] = ATREngine.calculate(
            market.highs, market.lows, market.closes
        )
        context.indicators["structure"] = MarketStructure.analyze(
            market.highs, market.lows
        )

    @staticmethod
    def _safe_stop(context, reason):

        decision = {
            "decision": "NO_TRADE",
            "reason": reason,
            "exchange": getattr(context, "exchange", None),
            "symbol": getattr(context, "symbol", None),
            "strategy": None,
        }

        context.decision = decision

        return decision
