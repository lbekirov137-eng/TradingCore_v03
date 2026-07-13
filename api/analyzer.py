from api.data_engine import DataEngine
from api.ema import EMAEngine
from api.rsi import RSIEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure
from api.signal_engine import SignalEngine
from api.risk_engine import RiskEngine
from api.trade_plan import TradePlan


class MarketAnalyzer:

    @staticmethod
    def analyze():

        market = DataEngine.load()

        closes = market["closes"]
        highs = market["highs"]
        lows = market["lows"]

        price = closes[-1]

        ema = EMAEngine.calculate_all(closes)
        rsi = RSIEngine.calculate(closes)
        atr = ATREngine.calculate(highs, lows, closes)
        structure = MarketStructure.analyze(highs, lows)

        signal = SignalEngine.generate(
            trend=ema["trend"],
            structure=structure,
            rsi=rsi,
        )

        risk = RiskEngine.calculate(
            balance=1000,
            risk_percent=0.1,
            price=price,
            atr=atr["value"],
        )

        trade_plan = TradePlan.build(
            signal=signal["signal"],
            price=price,
            atr=atr["value"],
        )

        return {
            "step": "FULL ENGINE",
            "signal": signal,
            "risk": risk,
            "trade_plan": trade_plan,
        }