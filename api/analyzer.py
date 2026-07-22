from api.data_engine import DataEngine
from api.ema import EMAEngine
from api.rsi import RSIEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure
from api.signal_engine import SignalEngine
from api.risk_engine import RiskEngine
from api.trade_plan import TradePlan

from api.contracts.context import MarketContext
from api.decision_engine.decision_engine import DecisionEngine
from api.core.bootstrap import Bootstrap

class MarketAnalyzer:

    @staticmethod
    def analyze(
        exchange: str = "binance",
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 300,
        balance: float = 1000,
        risk_percent: float = 0.1,
    ):

        # ==========================================
        # 1. Загрузка рыночных данных
        # ==========================================
        market = DataEngine.load(
            exchange=exchange,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        closes = market.closes
        highs = market.highs
        lows = market.lows

        price = closes[-1]

        # ==========================================
        # 2. Расчёт индикаторов
        # ==========================================
        ema = EMAEngine.calculate_all(closes)
        rsi = RSIEngine.calculate(closes)
        atr = ATREngine.calculate(...)
        structure = MarketStructure.analyze(...)

        # ==========================================
        # 3. Генерация сигнала стратегии
        # ==========================================
        signal = SignalEngine.generate(
            trend=ema["trend"],
            structure=structure,
            rsi=rsi,
        )

        # ==========================================
        # 4. Расчёт риска
        # ==========================================
        risk = RiskEngine.calculate(
            balance=balance,
            risk_percent=risk_percent,
            price=price,
            atr=atr["value"],
        )

        # ==========================================
        # 5. Построение торгового плана
        # ==========================================
        trade_plan = TradePlan.build(
            signal=signal["signal"],
            price=price,
            atr=atr["value"],
        )

        # ==========================================
        # 6. Создание единого MarketContext
        # ==========================================
        context = MarketContext()

        context.symbol = symbol
        context.exchange = exchange
        context.timeframe = interval

        context.market = {
            "timestamps": market.timestamps,
            "price": price,
            "closes": closes,
            "highs": highs,
            "lows": lows,
        }

        context.indicators = {
            "ema": ema,
            "rsi": rsi,
            "atr": atr,
            "structure": structure,
        }

        context.strategy = signal

        context.risk = risk

        context.execution = {
            "trade_plan": trade_plan
        }

        # ==========================================
        
        # 7. Core Engine
        # ==============================

        engine = Bootstrap.build()

        context = engine.execute(context)

        # ==========================================
        # 8. Ответ API
        # ==========================================
        return {
            "exchange": context.exchange,
            "symbol": context.symbol,
            "interval": context.timeframe,

            "timestamps": context.market["timestamps"],

            "price": round(context.market["price"], 2),

            "trend": context.indicators["ema"]["trend"],

            "structure": context.indicators["structure"],

            "ema": context.indicators["ema"],

            "rsi": context.indicators["rsi"],

            "atr": context.indicators["atr"],

            "signal": context.strategy,

            "risk": context.risk,
            "trade_plan": context.execution["trade_plan"],

            "decision": context.decision,
        }