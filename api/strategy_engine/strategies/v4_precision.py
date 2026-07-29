"""
SESSION_VWAP_RANGE_LOW_VOL_PX@4.0.0 — та же торговая логика, корректная
точность цены.

MIGRATION NOTE
==============
Это НЕ @3.0.0 и не может им считаться. Торговые правила идентичны:
режимные пороги, VWAP-логика, структурный стоп, target_rr, cost gate,
риск 0.1%, плечо 1x, LONG only — всё унаследовано без единого изменения.

Изменено ровно одно: уровни и количество округляются по правилам
ИНСТРУМЕНТА (tick size, step size), а не хардкодом до двух знаков.

Почему это всё равно новая версия. Округление меняет фактические entry,
stop и quantity, а значит и результат каждой сделки. Сравнивать числа
@4.0.0 с числами @3.0.0 нельзя: это разные выборки. Отсюда новый
strategy_key, новый parameter_hash и обязательный новый sample_id.

Что аннулировано. Все прежние результаты по инструментам, где хардкод
2 знаков схлопывал уровни — XRPUSDT, TRXUSDT, ADAUSDT — помечены как
INVALIDATED_BY_PRECISION_DEFECT. Их нельзя переиспользовать даже как
baseline: там отсутствуют 96 из 116 сигналов.

Что НЕ изменено: regime thresholds, strategy logic, cost model,
risk 0.1%, leverage 1x, max_cost_r 0.25, Champion.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.paper_trading.instrument_rules import (
    InstrumentRules,
    round_price_to_tick,
    round_quantity_to_step,
    rules_for,
    validate_order_geometry,
)
from api.strategy_engine.cost_gate import evaluate_cost_viability
from api.strategy_engine.strategies.contracts import StrategyDecision
from api.strategy_engine.strategies.v3_range_lowvol import (
    RangeLowVolConfig,
    SessionVwapRangeLowVol,
)
from api.strategy_engine.strategies.v2_structural import RISK_AMOUNT_USD


@dataclass(frozen=True)
class PrecisionConfig(RangeLowVolConfig):
    """
    Конфигурация @4.0.0.

    Отдельный класс -> отдельный parameter_hash. Все торговые параметры
    унаследованы без изменения; добавлено только поле точности.
    """

    respect_instrument_precision: bool = True
    max_rr_distortion: float = 0.02


class SessionVwapRangeLowVolPrecision(SessionVwapRangeLowVol):

    strategy_key = "SESSION_VWAP_RANGE_LOW_VOL_PX"
    version = "4.0.0"

    def __init__(
        self,
        config: PrecisionConfig | None = None,
        cost_gate_config=None,
        symbol: str = "BTCUSDT",
    ) -> None:
        super().__init__(config or PrecisionConfig(), cost_gate_config)
        self.symbol = symbol
        self.rules: InstrumentRules = rules_for(symbol)

    def finalise(
        self,
        entry: float,
        stop: float,
        atr_value: float,
        reason_code: str,
        **diagnostics,
    ) -> StrategyDecision:
        """
        Тот же порядок проверок, что в @2.0.0/@3.0.0, но с округлением по
        правилам инструмента и явной проверкой геометрии.

        Порядок принципиален: сначала структурная допустимость стопа в ATR
        (до округления — округление не должно влиять на торговое решение),
        затем округление, затем геометрия, затем экономика.
        """
        config = self.config
        rules = self.rules

        if stop <= 0 or stop >= entry:
            return self.no_trade("INVALID_STRUCTURAL_STOP", stop=stop, entry=entry)

        stop_in_atr = (entry - stop) / atr_value

        if stop_in_atr < config.min_stop_atr:
            return self.no_trade(
                "STRUCTURAL_STOP_TOO_TIGHT",
                stop_in_atr=round(stop_in_atr, 3),
                minimum=config.min_stop_atr, **diagnostics,
            )

        if stop_in_atr > config.max_stop_atr:
            return self.no_trade(
                "STRUCTURAL_STOP_TOO_WIDE",
                stop_in_atr=round(stop_in_atr, 3),
                maximum=config.max_stop_atr, **diagnostics,
            )

        raw_stop_distance = entry - stop
        raw_take_profit = entry + config.target_rr * raw_stop_distance
        raw_tp1 = entry + 2.0 * raw_stop_distance

        # Округление по сетке инструмента.
        # entry — к ближайшему тику; stop — ВНИЗ (риск чуть больше, а не
        # тише заявленного); цели — вниз (берём достижимое, не желаемое).
        px_entry = round_price_to_tick(entry, rules, "nearest")
        px_stop = round_price_to_tick(stop, rules, "down")
        px_tp1 = round_price_to_tick(raw_tp1, rules, "down")
        px_tp2 = round_price_to_tick(raw_take_profit, rules, "down")

        stop_distance = px_entry - px_stop

        if stop_distance <= 0:
            return self.no_trade(
                "LEVELS_COLLAPSED",
                entry=px_entry, stop=px_stop,
                tick_size=rules.tick_size, symbol=rules.symbol,
                **diagnostics,
            )

        quantity = round_quantity_to_step(
            RISK_AMOUNT_USD / stop_distance, rules
        )

        geometry = validate_order_geometry(
            entry=px_entry, stop=px_stop,
            take_profit_1=px_tp1, take_profit_2=px_tp2,
            quantity=quantity, rules=rules,
            intended_rr=config.target_rr,
            max_rr_distortion=config.max_rr_distortion,
        )

        if not geometry["valid"]:
            return self.no_trade(
                geometry["reason"],
                symbol=rules.symbol, tick_size=rules.tick_size,
                step_size=rules.step_size,
                realised_rr=geometry.get("realised_rr"),
                notional=geometry.get("notional"),
                **diagnostics,
            )

        viability = evaluate_cost_viability(
            entry=px_entry, stop=px_stop, take_profit=px_tp2,
            risk_amount=RISK_AMOUNT_USD,
            config=self.cost_gate_config,
        )

        if not viability["viable"]:
            return self.no_trade(
                viability["reason_code"],
                estimated_cost_r=viability["estimated_cost_r"],
                net_rr_after_costs=viability["net_rr_after_costs"],
                stop_in_atr=round(stop_in_atr, 3), **diagnostics,
            )

        # Фактический риск после округления количества вниз может быть
        # НИЖЕ заявленного — это безопасно. Выше он быть не может.
        actual_risk = quantity * stop_distance

        return StrategyDecision(
            strategy_key=self.strategy_key,
            version=self.version,
            signal="BUY",
            reason_code=reason_code,
            entry=px_entry, stop=px_stop,
            take_profit_1=px_tp1, take_profit_2=px_tp2,
            risk_reward=round(geometry["realised_rr"], 4),
            diagnostics={
                **diagnostics,
                "symbol": rules.symbol,
                "tick_size": rules.tick_size,
                "step_size": rules.step_size,
                "stop_in_atr": round(stop_in_atr, 3),
                "quantity": quantity,
                "position_notional": round(quantity * px_entry, 8),
                "actual_risk_usd": round(actual_risk, 8),
                "estimated_cost_r": viability["estimated_cost_r"],
                "net_rr_after_costs": viability["net_rr_after_costs"],
                "risk_amount": RISK_AMOUNT_USD,
                "leverage": 1,
            },
        )
