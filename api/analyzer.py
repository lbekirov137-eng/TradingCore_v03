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
    def analyze(
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 300,
        balance: float = 1000,
        risk_percent: float = 0.1,
    ):

        market = DataEngine.load(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

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
            balance=balance,
            risk_percent=risk_percent,
            price=price,
            atr=atr["value"],
        )

        trade_plan = TradePlan.build(
            signal=signal["signal"],
            price=price,
            atr=atr["value"],
        )

        return {
            "symbol": symbol,
            "interval": interval,
            "price": round(price, 2),
            "trend": ema["trend"],
            "structure": structure,
            "ema": ema,
            "rsi": rsi,
            "atr": atr,
            "signal": signal,
            "risk": risk,
            "trade_plan": trade_plan,
        }