"""
Тесты слоя точности и версии @4.0.0.

Центральная регрессия — TRXUSDT: при хардкоде в 2 знака 89 из 108 его
сигналов исчезали, потому что entry и stop схлопывались в одно значение.
Реальный тик TRX равен 0.0001, а не 0.01.
"""

import dataclasses

import pytest

from api.paper_trading.instrument_rules import (
    BELOW_MIN_NOTIONAL,
    BELOW_MIN_QUANTITY,
    FALLBACK_RULES,
    GEOMETRY_OK,
    INSTRUMENT_RULES,
    LEVELS_COLLAPSED,
    RR_DISTORTED_BY_ROUNDING,
    TARGET_COLLAPSED,
    InstrumentRules,
    round_price_to_tick,
    round_quantity_to_step,
    rules_for,
    validate_order_geometry,
)
from api.strategy_engine.strategies.v3_range_lowvol import (
    RangeLowVolConfig,
    SessionVwapRangeLowVol,
)
from api.strategy_engine.strategies.v4_precision import (
    PrecisionConfig,
    SessionVwapRangeLowVolPrecision,
)

REQUIRED = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT",
            "XRPUSDT", "TRXUSDT", "SOLUSDT", "LTCUSDT"]

# Ориентировочные рыночные цены для проверки геометрии.
PRICES = {"BTCUSDT": 68000.0, "ETHUSDT": 3500.0, "BNBUSDT": 600.0,
          "ADAUSDT": 0.45, "XRPUSDT": 0.52, "TRXUSDT": 0.11,
          "SOLUSDT": 150.0, "LTCUSDT": 85.0}


class TestRulesCoverage:

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_every_required_instrument_has_rules(self, symbol) -> None:
        r = rules_for(symbol)

        assert r.symbol == symbol
        assert r.tick_size > 0
        assert r.step_size > 0
        assert r.min_notional > 0
        assert "exchangeInfo" in r.source

    def test_precision_is_derived_from_tick_not_price(self) -> None:
        """
        Ключевое свойство: точность задаётся тиком биржи. TRX и BTC стоят
        по-разному, но точность НЕ выводится из цены.
        """
        assert rules_for("TRXUSDT").price_precision == 4
        assert rules_for("BTCUSDT").price_precision == 2
        assert rules_for("XRPUSDT").price_precision == 4

    def test_unknown_symbol_gets_explicit_fallback(self) -> None:
        r = rules_for("NOSUCHUSDT")

        assert r is FALLBACK_RULES
        assert "FALLBACK" in r.source

    def test_rules_are_immutable(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            INSTRUMENT_RULES["BTCUSDT"].tick_size = 1.0  # type: ignore[misc]


class TestRounding:

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_price_lands_on_tick_grid(self, symbol) -> None:
        r = rules_for(symbol)
        price = PRICES[symbol] * 1.0007

        for mode in ("nearest", "down", "up"):
            p = round_price_to_tick(price, r, mode)
            units = round(p / r.tick_size)

            assert abs(p - units * r.tick_size) < r.tick_size * 1e-6

    def test_stop_rounds_down_so_risk_is_never_understated(self) -> None:
        """Округление стопа вверх тихо уменьшило бы риск ниже заявленного."""
        r = rules_for("BTCUSDT")

        assert round_price_to_tick(67000.567, r, "down") <= 67000.567

    def test_quantity_always_rounds_down(self) -> None:
        r = rules_for("ADAUSDT")   # step 0.1

        assert round_quantity_to_step(12.99, r) == pytest.approx(12.9)
        assert round_quantity_to_step(0.05, r) == pytest.approx(0.0)

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_rounding_is_deterministic(self, symbol) -> None:
        r = rules_for(symbol)
        p = PRICES[symbol] * 1.00031

        assert round_price_to_tick(p, r) == round_price_to_tick(p, r)
        assert round_quantity_to_step(7.77, r) == round_quantity_to_step(7.77, r)


class TestGeometry:

    def build(self, symbol, stop_pct=0.012, rr=3.0):
        r = rules_for(symbol)
        price = PRICES[symbol]
        entry = round_price_to_tick(price, r)
        stop = round_price_to_tick(price * (1 - stop_pct), r, "down")
        d = entry - stop
        tp1 = round_price_to_tick(entry + 2 * d, r, "down")
        tp2 = round_price_to_tick(entry + rr * d, r, "down")
        qty = round_quantity_to_step(1.0 / d, r) if d > 0 else 0.0
        return r, entry, stop, tp1, tp2, qty

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_levels_never_collapse_after_fix(self, symbol) -> None:
        """
        Регрессия дефекта. При хардкоде в 2 знака TRX/XRP/ADA схлопывались.
        """
        r, entry, stop, tp1, tp2, qty = self.build(symbol)

        assert entry != stop, f"{symbol}: entry collapsed onto stop"
        assert entry - stop > 0
        assert tp1 > entry
        assert tp2 > tp1

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_full_geometry_validates(self, symbol) -> None:
        r, entry, stop, tp1, tp2, qty = self.build(symbol)

        g = validate_order_geometry(
            entry=entry, stop=stop, take_profit_1=tp1, take_profit_2=tp2,
            quantity=qty, rules=r, intended_rr=3.0,
        )

        assert g["valid"] is True, (symbol, g["reason"])
        assert g["reason"] == GEOMETRY_OK
        assert g["quantity"] > 0
        assert g["notional"] >= r.min_notional

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_quantity_is_never_zero(self, symbol) -> None:
        _, _, _, _, _, qty = self.build(symbol)

        assert qty > 0, f"{symbol}: quantity rounded to zero"

    def test_collapsed_levels_are_rejected_with_reason(self) -> None:
        r = rules_for("TRXUSDT")

        g = validate_order_geometry(
            entry=0.11, stop=0.11, take_profit_1=0.12, take_profit_2=0.13,
            quantity=10.0, rules=r,
        )

        assert g["valid"] is False
        assert g["reason"] == LEVELS_COLLAPSED

    def test_target_collapse_is_rejected(self) -> None:
        r = rules_for("BTCUSDT")

        g = validate_order_geometry(
            entry=68000.0, stop=67000.0, take_profit_1=68000.0,
            take_profit_2=69000.0, quantity=0.001, rules=r,
        )

        assert g["valid"] is False
        assert g["reason"] == TARGET_COLLAPSED

    def test_below_min_quantity_is_rejected(self) -> None:
        r = rules_for("ADAUSDT")

        g = validate_order_geometry(
            entry=0.45, stop=0.44, take_profit_1=0.47, take_profit_2=0.48,
            quantity=0.0, rules=r,
        )

        assert g["valid"] is False
        assert g["reason"] in (BELOW_MIN_QUANTITY, "QUANTITY_ZERO")

    def test_below_min_notional_is_rejected(self) -> None:
        r = rules_for("BTCUSDT")

        g = validate_order_geometry(
            entry=68000.0, stop=67000.0, take_profit_1=70000.0,
            take_profit_2=71000.0, quantity=1e-05, rules=r,
        )

        assert g["valid"] is False
        assert g["reason"] == BELOW_MIN_NOTIONAL

    def test_rr_distortion_beyond_threshold_is_rejected(self) -> None:
        """
        Округление всегда сдвигает R:R. Вопрос в величине: заявленный
        R:R обязан описывать реальную сделку.
        """
        r = rules_for("BTCUSDT")

        g = validate_order_geometry(
            entry=68000.0, stop=67900.0, take_profit_1=68200.0,
            take_profit_2=68150.0, quantity=0.01, rules=r,
            intended_rr=3.0,
        )

        assert g["valid"] is False
        assert g["reason"] in (RR_DISTORTED_BY_ROUNDING, TARGET_COLLAPSED)

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_rr_distortion_within_threshold(self, symbol) -> None:
        r, entry, stop, tp1, tp2, qty = self.build(symbol)

        g = validate_order_geometry(
            entry=entry, stop=stop, take_profit_1=tp1, take_profit_2=tp2,
            quantity=qty, rules=r, intended_rr=3.0,
        )

        assert g["rr_distortion"] <= 0.02, (symbol, g["rr_distortion"])


class TestVersionIsolation:

    def test_v4_has_new_key_and_version(self) -> None:
        assert SessionVwapRangeLowVolPrecision.strategy_key == (
            "SESSION_VWAP_RANGE_LOW_VOL_PX"
        )
        assert SessionVwapRangeLowVolPrecision.version == "4.0.0"

    def test_v3_is_untouched(self) -> None:
        assert SessionVwapRangeLowVol.strategy_key == "SESSION_VWAP_RANGE_LOW_VOL"
        assert SessionVwapRangeLowVol.version == "3.0.0"
        assert RangeLowVolConfig().fingerprint() == "8a80889064e57538"

    def test_parameter_hash_differs(self) -> None:
        assert PrecisionConfig().fingerprint() != RangeLowVolConfig().fingerprint()

    def test_trading_parameters_are_inherited_unchanged(self) -> None:
        """Изменена ТОЛЬКО точность. Торговые правила те же."""
        a, b = PrecisionConfig(), RangeLowVolConfig()

        for field in ("range_ema_gap_max_pct", "low_vol_atr_max_pct",
                      "low_vol_atr_min_pct", "target_rr", "min_stop_atr",
                      "max_stop_atr", "warmup_bars", "structure_lookback",
                      "max_trades_per_session", "execution_timeframe",
                      "context_timeframe"):
            assert getattr(a, field) == getattr(b, field), field

    def test_cost_gate_unchanged(self) -> None:
        assert SessionVwapRangeLowVolPrecision().cost_gate_config.max_cost_r == 0.25

    def test_config_immutable(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            PrecisionConfig().max_rr_distortion = 1.0  # type: ignore[misc]

    @pytest.mark.parametrize("symbol", REQUIRED)
    def test_strategy_binds_correct_rules(self, symbol) -> None:
        s = SessionVwapRangeLowVolPrecision(symbol=symbol)

        assert s.rules.symbol == symbol
        assert s.symbol == symbol


class TestChampionUnchanged:

    def test_champion_is_still_range_no_trade(self) -> None:
        from api.strategy_supervisor import DEFAULT_STRATEGY_ID

        assert DEFAULT_STRATEGY_ID == "RANGE_NO_TRADE_POLICY"
