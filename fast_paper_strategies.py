#!/usr/bin/env python3
"""Frozen strategy definitions for Fast PAPER Lab V1.

These rules are copied from the preregistered Frequent Edge research family and
are frozen for NEW forward PAPER evidence. No runtime parameters or optimizers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.strategy_engine.cost_gate import evaluate_cost_viability
from api.strategy_engine.strategies.contracts import (
    BaseStrategy,
    CandleWindow,
    StrategyDecision,
    atr,
    ema,
    session_vwap,
)
from fast_paper_protocol import CAPITAL_USD if False else REFERENCE_CAPITAL_USD
from fast_paper_protocol import RISK_AMOUNT_USD

MIN_RR = 2.0


def _session_slice(window: CandleWindow) -> list:
    current_day = datetime.fromtimestamp(
        window.current.open_time_ms / 1000.0, tz=timezone.utc
    ).date()
    rows = []
    for offset in range(len(window)):
        candle = window[-1 - offset]
        day = datetime.fromtimestamp(
            candle.open_time_ms / 1000.0, tz=timezone.utc
        ).date()
        if day != current_day:
            break
        rows.append(candle)
    rows.reverse()
    return rows


class FastMeanReversionBase(BaseStrategy):
    version = "1.0.0-fast-paper-frozen"

    def _common(self, window: CandleWindow) -> dict[str, Any] | None:
        history = window.slice(self.config.warmup_bars)
        atr_value = atr(history, self.config.atr_period)
        if atr_value is None or atr_value <= 0:
            return None
        closes = window.closes(self.config.warmup_bars)
        fast = ema(closes, 20)
        slow = ema(closes, 50)
        if fast is None or slow is None:
            return None
        gap_pct = abs(fast - slow) / window.current.close * 100.0
        atr_pct = atr_value / window.current.close * 100.0
        return {
            "atr": atr_value,
            "fast": fast,
            "slow": slow,
            "gap_pct": gap_pct,
            "atr_pct": atr_pct,
        }

    def _finalise(
        self,
        *,
        entry: float,
        stop: float,
        target: float,
        reason: str,
        diagnostics: dict[str, Any],
    ) -> StrategyDecision:
        if not (stop > 0 and stop < entry < target):
            return self.no_trade("INVALID_GEOMETRY", **diagnostics)

        risk_per_unit = entry - stop
        rr = (target - entry) / risk_per_unit
        if rr < MIN_RR:
            return self.no_trade("RISK_REWARD_BELOW_MINIMUM", **diagnostics)

        position_notional = (RISK_AMOUNT_USD / risk_per_unit) * entry
        if position_notional > REFERENCE_CAPITAL_USD + 1e-9:
            return self.no_trade(
                "STOP_TOO_TIGHT_FOR_1X_SPOT",
                position_notional=round(position_notional, 2),
                **diagnostics,
            )

        viability = evaluate_cost_viability(
            entry=entry,
            stop=stop,
            take_profit=target,
            risk_amount=RISK_AMOUNT_USD,
        )
        if not viability.get("viable"):
            return self.no_trade(
                viability.get("reason_code", "COST_GATE_REJECTED"),
                estimated_cost_r=viability.get("estimated_cost_r"),
                net_rr_after_costs=viability.get("net_rr_after_costs"),
                **diagnostics,
            )

        px_entry = round(float(entry), 2)
        px_stop = round(float(stop), 2)
        px_target = round(float(target), 2)
        px_tp1 = round(px_entry + (px_entry - px_stop), 2)
        if not (px_stop < px_entry < px_tp1 < px_target):
            return self.no_trade("ROUNDING_COLLAPSED_GEOMETRY", **diagnostics)

        realised_rr = (px_target - px_entry) / (px_entry - px_stop)
        if realised_rr < MIN_RR:
            return self.no_trade("ROUNDING_RR_BELOW_MINIMUM", **diagnostics)

        return StrategyDecision(
            strategy_key=self.strategy_key,
            version=self.version,
            signal="BUY",
            reason_code=reason,
            entry=px_entry,
            stop=px_stop,
            take_profit_1=px_tp1,
            take_profit_2=px_target,
            risk_reward=round(realised_rr, 4),
            diagnostics={
                **diagnostics,
                "estimated_cost_r": viability.get("estimated_cost_r"),
                "net_rr_after_costs": viability.get("net_rr_after_costs"),
                "position_notional": round(position_notional, 2),
                "risk_amount": RISK_AMOUNT_USD,
                "leverage": 1,
            },
        )


class FastVwapOvershootReclaim(FastMeanReversionBase):
    strategy_key = "MR_VWAP_OVERSHOOT_RECLAIM"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 3:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")
        if common["gap_pct"] > 0.80:
            return self.no_trade("TREND_TOO_STRONG")

        session = _session_slice(window)
        if len(session) < 4:
            return self.no_trade("SESSION_TOO_SHORT")
        anchor = session_vwap(session[:-1])
        if anchor is None:
            return self.no_trade("VWAP_UNAVAILABLE")

        previous = window[-2]
        current = window.current
        atr_value = common["atr"]
        if previous.close >= anchor - 0.60 * atr_value:
            return self.no_trade("NO_VWAP_OVERSHOOT")
        if not (current.close > current.open and current.close > previous.close):
            return self.no_trade("NO_BULLISH_RECLAIM")
        if current.close >= anchor:
            return self.no_trade("REVERSION_ALREADY_COMPLETE")

        stop = min(window[-1].low, window[-2].low, window[-3].low) - 0.10 * atr_value
        return self._finalise(
            entry=current.close,
            stop=stop,
            target=anchor,
            reason="VWAP_OVERSHOOT_RECLAIM_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "session_vwap": round(anchor, 4),
            },
        )


class FastAtrFlushReversal(FastMeanReversionBase):
    strategy_key = "MR_ATR_FLUSH_REVERSAL"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < 4:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")
        if common["gap_pct"] > 1.00:
            return self.no_trade("TREND_TOO_STRONG")

        before = window[-3]
        previous = window[-2]
        current = window.current
        atr_value = common["atr"]
        drop_atr = (before.close - previous.close) / atr_value
        if drop_atr < 1.25:
            return self.no_trade("FLUSH_TOO_SMALL")

        previous_mid = (previous.high + previous.low) / 2.0
        if not (current.close > current.open and current.close > previous_mid):
            return self.no_trade("REVERSAL_NOT_CONFIRMED")

        stop = min(previous.low, current.low) - 0.15 * atr_value
        risk = current.close - stop
        if risk <= 0:
            return self.no_trade("INVALID_RISK")
        target = current.close + 2.50 * risk
        return self._finalise(
            entry=current.close,
            stop=stop,
            target=target,
            reason="ATR_FLUSH_REVERSAL_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "drop_atr": round(drop_atr, 4),
            },
        )


class FastEmaBandReentry(FastMeanReversionBase):
    strategy_key = "MR_EMA20_BAND_REENTRY"

    def _evaluate(self, window: CandleWindow) -> StrategyDecision:
        common = self._common(window)
        if common is None or len(window) < self.config.warmup_bars + 1:
            return self.no_trade("COMMON_DATA_UNAVAILABLE")

        closes_now = window.closes(self.config.warmup_bars)
        closes_prev = window.slice(self.config.warmup_bars + 1)[:-1]
        ema_now = ema(closes_now, 20)
        ema_prev = ema([c.close for c in closes_prev], 20)
        if ema_now is None or ema_prev is None:
            return self.no_trade("EMA_UNAVAILABLE")

        current = window.current
        previous = window[-2]
        atr_value = common["atr"]
        slope_pct = abs(ema_now - ema_prev) / current.close * 100.0
        if common["gap_pct"] > 0.80 or slope_pct > 0.20:
            return self.no_trade("REGIME_NOT_FLAT")
        if previous.close >= ema_prev - 1.00 * atr_value:
            return self.no_trade("NO_LOWER_BAND_OVERSHOOT")
        if not (current.close > current.open and current.close > ema_now - 0.50 * atr_value):
            return self.no_trade("NO_BAND_REENTRY")
        if current.close >= ema_now:
            return self.no_trade("REVERSION_ALREADY_COMPLETE")

        stop = min(previous.low, current.low) - 0.10 * atr_value
        return self._finalise(
            entry=current.close,
            stop=stop,
            target=ema_now,
            reason="EMA20_BAND_REENTRY_CONFIRMED",
            diagnostics={
                "atr_percent": round(common["atr_pct"], 4),
                "ema_gap_percent": round(common["gap_pct"], 4),
                "ema20_slope_percent": round(slope_pct, 4),
                "ema20": round(ema_now, 4),
            },
        )


STRATEGY_CLASSES = {
    "MR_VWAP_OVERSHOOT_RECLAIM": FastVwapOvershootReclaim,
    "MR_ATR_FLUSH_REVERSAL": FastAtrFlushReversal,
    "MR_EMA20_BAND_REENTRY": FastEmaBandReentry,
}
