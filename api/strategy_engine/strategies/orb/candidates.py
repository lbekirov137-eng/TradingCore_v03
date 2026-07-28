"""
Кандидаты для исследования ORB — КАЖДЫЙ оценивается ОТДЕЛЬНО против
базовой стратегии, ни один не объединяется с другими в одну "супер"
версию (явное требование: не комбинировать все фильтры в одну
переобученную стратегию). Ни один кандидат не заменяет baseline
ORBStrategy без отдельного, задокументированного решения на основе
задокументированных бэктестов.
"""

from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy
from api.strategy_engine.strategies.orb.opening_range import OpeningRange


class ORBWithRangeATRFilter:
    """
    Кандидат: отклоняет сигнал, если ширина Opening Range слишком мала
    или слишком велика относительно ATR (слишком узкий диапазон даёт
    много ложных пробоев; слишком широкий — стоп относительно входа
    становится непропорционально большим).
    """

    NAME = "ORB_RANGE_ATR_FILTER"

    def __init__(self, min_ratio: float = 0.3, max_ratio: float = 3.0):
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio

    def generate(self, context):

        opening_range = OpeningRange.calculate(context)

        if opening_range is None:
            return ORBStrategy.generate(context)  # let baseline produce its own no-trade reason

        atr = (context.indicators.get("atr") or {}).get("value")

        if atr is None or atr != atr or atr <= 0:
            return ORBStrategy.generate(context)

        ratio = opening_range["range"] / atr

        if not (self.min_ratio <= ratio <= self.max_ratio):
            return {
                "approved": False,
                "strategy": self.NAME,
                "direction": None,
                "trade_plan": None,
                "confidence": 0.0,
                "reason": f"Range/ATR={ratio:.2f} вне допустимого окна [{self.min_ratio}, {self.max_ratio}].",
                "metadata": {"opening_range": opening_range, "range_atr_ratio": ratio},
            }

        result = ORBStrategy.generate(context)
        result["strategy"] = self.NAME
        return result


class ORBWithMinRelativeVolume:
    """
    Кандидат: требует, чтобы объём свечи пробоя был не ниже заданной
    доли среднего объёма за последние 20 свечей -- отфильтровывает
    пробои на аномально низком объёме (часто ложные).
    """

    NAME = "ORB_MIN_RELATIVE_VOLUME"

    def __init__(self, min_volume_ratio: float = 1.0):
        self.min_volume_ratio = min_volume_ratio

    def generate(self, context):

        market = context.visible_market
        volumes = market.volumes

        if len(volumes) < 21:
            return ORBStrategy.generate(context)

        recent = volumes[-1]
        average = sum(volumes[-21:-1]) / 20

        if average <= 0:
            return ORBStrategy.generate(context)

        ratio = recent / average

        if ratio < self.min_volume_ratio:
            return {
                "approved": False,
                "strategy": self.NAME,
                "direction": None,
                "trade_plan": None,
                "confidence": 0.0,
                "reason": f"Объём пробоя {ratio:.2f}x ниже требуемых {self.min_volume_ratio}x среднего.",
                "metadata": {"volume_ratio": ratio},
            }

        result = ORBStrategy.generate(context)
        result["strategy"] = self.NAME
        return result
