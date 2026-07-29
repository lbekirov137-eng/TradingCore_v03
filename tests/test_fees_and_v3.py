"""
Тесты FeeProfile, maker-исполнения и стратегии @3.0.0.

Главное проверяемое свойство первой группы: рекламный минимальный тариф не
должен выглядеть достижимым. Именно эта подмена делает нежизнеспособную
стратегию прибыльной на бумаге.
"""

import dataclasses

import pytest

from api.paper_trading.fee_profiles import (
    ALL_PROFILES,
    BINANCE_BNB,
    BINANCE_VIP3,
    HYPOTHETICAL_MAKER_002,
    OKX_LV1,
    achievable_profiles,
    best_achievable_maker,
    profiles_snapshot,
)
from api.strategy_engine.maker_execution import (
    MakerExecutionConfig,
    simulate_maker_entry,
    summarise_fills,
)
from api.strategy_engine.strategies.contracts import Candle
from api.strategy_engine.strategies.v2_structural import StructuralConfig
from api.strategy_engine.strategies.v3_range_lowvol import (
    RangeLowVolConfig,
    SessionVwapRangeLowVol,
)
from api.strategy_engine.strategies.v2_structural import SessionVwapTrendPullbackV2

HOUR = 3_600_000


def bar(t, close, low=None, high=None, vol=10.0):
    return Candle(open_time_ms=t, open=close,
                  high=high if high is not None else close + 1,
                  low=low if low is not None else close - 1,
                  close=close, volume=vol)


class TestFeeProfiles:

    def test_every_profile_carries_a_source_and_date(self) -> None:
        for p in ALL_PROFILES:
            assert p.source_reference
            assert p.source_date
            assert p.confidence

    def test_vip_tiers_are_not_marked_achievable(self) -> None:
        """Тариф может быть верным и при этом недостижимым."""
        assert BINANCE_VIP3.achievable_now is False
        assert HYPOTHETICAL_MAKER_002.achievable_now is False
        assert "400" in HYPOTHETICAL_MAKER_002.requirements

    def test_no_profile_is_owner_verified_yet(self) -> None:
        """Пока владелец не подтвердил, ни один тариф не считается фактом."""
        assert all(p.verified_by_owner is False for p in ALL_PROFILES)

    def test_best_achievable_excludes_hypothetical_tiers(self) -> None:
        best = best_achievable_maker()

        assert best.achievable_now is True
        assert best.maker_fee >= 0.00075, (
            "a sub-0.075% maker fee must not be presented as achievable"
        )

    def test_002_percent_maker_is_not_achievable(self) -> None:
        """
        0.02% часто цитируется как «комиссия мейкера». Она требует объёма
        VIP 6+, которого у нас нет.
        """
        assert HYPOTHETICAL_MAKER_002 not in achievable_profiles()

    def test_okx_has_lowest_unconditional_maker(self) -> None:
        unconditional = [
            p for p in achievable_profiles() if p.requirements == "none"
        ]
        assert OKX_LV1.maker_fee < min(p.maker_fee for p in unconditional)

    def test_binance_bnb_requires_holding_bnb(self) -> None:
        assert "BNB" in BINANCE_BNB.requirements
        assert BINANCE_BNB.discount_source

    def test_profile_is_immutable(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            OKX_LV1.maker_fee = 0.0  # type: ignore[misc]

    def test_snapshot_serialises(self) -> None:
        snap = profiles_snapshot()

        assert len(snap) == len(ALL_PROFILES)
        assert all("achievable_now" in row for row in snap)


class TestMakerExecution:

    def test_order_fills_when_price_reaches_limit(self) -> None:
        candles = [bar(i * HOUR, 100.0) for i in range(5)]
        candles[2] = bar(2 * HOUR, 99.0, low=98.0)

        result = simulate_maker_entry(candles, 0, limit_price=98.5)

        assert result["filled"] is True
        assert result["wait_bars"] == 2

    def test_order_is_cancelled_on_timeout(self) -> None:
        candles = [bar(i * HOUR, 100.0, low=99.5) for i in range(10)]

        result = simulate_maker_entry(
            candles, 0, limit_price=90.0,
            config=MakerExecutionConfig(timeout_bars=3),
        )

        assert result["filled"] is False
        assert result["status"] == "CANCELLED_TIMEOUT"
        assert result["wait_bars"] == 3

    def test_adverse_selection_is_detected(self) -> None:
        """Исполнены на падении: свеча закрылась ниже нашего лимита."""
        candles = [bar(i * HOUR, 100.0) for i in range(5)]
        candles[1] = bar(HOUR, 97.0, low=96.0)

        result = simulate_maker_entry(candles, 0, limit_price=98.0)

        assert result["filled"] is True
        assert result["adverse"] is True

    def test_no_adverse_flag_when_price_recovers(self) -> None:
        candles = [bar(i * HOUR, 100.0) for i in range(5)]
        candles[1] = bar(HOUR, 99.5, low=97.5)

        result = simulate_maker_entry(candles, 0, limit_price=98.0)

        assert result["filled"] is True
        assert result["adverse"] is False

    def test_missed_order_is_not_a_trade(self) -> None:
        results = [
            {"filled": True, "status": "FILLED", "wait_bars": 1, "adverse": False},
            {"filled": False, "status": "CANCELLED_TIMEOUT", "wait_bars": 3,
             "adverse": False},
            {"filled": False, "status": "CANCELLED_TIMEOUT", "wait_bars": 3,
             "adverse": False},
        ]

        summary = summarise_fills(results)

        assert summary["signals"] == 3
        assert summary["filled_orders"] == 1
        assert summary["missed"] == 2
        assert summary["fill_rate_percent"] == pytest.approx(33.33, abs=0.01)

    def test_taker_fallback_is_disabled_by_default(self) -> None:
        assert MakerExecutionConfig().allow_taker_fallback is False
        assert summarise_fills(
            [{"filled": True, "status": "FILLED", "wait_bars": 1,
              "adverse": False}]
        )["taker_fallback_used"] == 0

    def test_end_of_data_is_a_miss_not_a_fill(self) -> None:
        candles = [bar(i * HOUR, 100.0, low=99.9) for i in range(2)]

        result = simulate_maker_entry(candles, 0, limit_price=50.0)

        assert result["filled"] is False
        assert "MISSED" in result["status"] or "CANCEL" in result["status"]


class TestV3StrategyIsolation:

    def test_v3_has_new_key_and_version(self) -> None:
        assert SessionVwapRangeLowVol.strategy_key == "SESSION_VWAP_RANGE_LOW_VOL"
        assert SessionVwapRangeLowVol.version == "3.0.0"

    def test_v2_is_unchanged(self) -> None:
        assert SessionVwapTrendPullbackV2.strategy_key == (
            "SESSION_VWAP_TREND_PULLBACK_V2"
        )
        assert SessionVwapTrendPullbackV2.version == "2.0.0"

    def test_parameter_hash_differs_from_v2(self) -> None:
        assert RangeLowVolConfig().fingerprint() != StructuralConfig().fingerprint()

    def test_derived_from_prior_analysis_is_declared(self) -> None:
        """
        Честность фиксируется в коде: пороги подсказаны прошлой выборкой,
        поэтому единственная валидная проверка — untouched holdout.
        """
        assert RangeLowVolConfig().derived_from_prior_analysis is True

    def test_config_is_immutable(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            RangeLowVolConfig().range_ema_gap_max_pct = 5.0  # type: ignore[misc]

    def test_thresholds_match_the_frozen_specification(self) -> None:
        c = RangeLowVolConfig()

        assert c.range_ema_gap_max_pct == 0.40
        assert c.low_vol_atr_max_pct == 0.45
        assert c.low_vol_atr_min_pct == 0.10
        assert c.max_trades_per_session == 1
        assert c.execution_timeframe == "1H"
        assert c.context_timeframe == "4H"

    def test_cost_gate_is_not_weakened(self) -> None:
        assert SessionVwapRangeLowVol().cost_gate_config.max_cost_r == 0.25


class TestV3RegimeFilter:

    def flat_series(self, n=120):
        return [bar(i * HOUR, 100.0, low=99.9, high=100.1) for i in range(n)]

    def trending_series(self, n=120):
        return [bar(i * HOUR, 100.0 + i * 2.0) for i in range(n)]

    def test_strong_trend_is_refused_as_not_range(self) -> None:
        s = SessionVwapRangeLowVol()
        candles = self.trending_series()

        d = s.evaluate_with_context(candles, 110, [])

        assert d.signal == "NO_TRADE"
        assert d.reason_code == "REGIME_NOT_ELIGIBLE"
        assert d.diagnostics.get("regime") in (
            "NOT_RANGE", "NOT_LOW_VOL", "VOLATILITY_TOO_DEAD"
        )

    def test_quiet_flat_market_is_the_target_regime(self) -> None:
        """
        Тихий боковик — это ИМЕННО целевой режим (ATR 0.2%, EMA gap 0).
        Фильтр его пропускает; отказ приходит позже и по другой причине.
        Проверяем это явно, чтобы фильтр случайно не «починили» наоборот.
        """
        s = SessionVwapRangeLowVol()

        d = s.evaluate_with_context(self.flat_series(), 110, [])

        assert d.diagnostics.get("regime") == "RANGE_LOW_VOL"
        assert d.reason_code != "REGIME_NOT_ELIGIBLE"

    def test_truly_dead_market_is_refused(self) -> None:
        """ATR ниже нижней границы: движения нет вообще."""
        s = SessionVwapRangeLowVol()
        # диапазон ~0.002% цены -> заведомо ниже low_vol_atr_min_pct=0.10
        candles = [
            bar(i * HOUR, 100.0, low=99.999, high=100.001) for i in range(120)
        ]

        d = s.evaluate_with_context(candles, 110, [])

        assert d.signal == "NO_TRADE"
        assert d.reason_code == "REGIME_NOT_ELIGIBLE"
        assert d.diagnostics.get("regime") == "VOLATILITY_TOO_DEAD"

    def test_downtrend_is_also_not_range(self) -> None:
        """abs() важен: сильное падение — не боковик."""
        s = SessionVwapRangeLowVol()
        candles = [bar(i * HOUR, 300.0 - i * 2.0) for i in range(120)]

        d = s.evaluate_with_context(candles, 110, [])

        assert d.signal == "NO_TRADE"

    def test_never_emits_short(self) -> None:
        s = SessionVwapRangeLowVol()

        for series in (self.flat_series(), self.trending_series()):
            for i in range(60, len(series), 10):
                d = s.evaluate_with_context(series, i, [])
                assert d.signal in ("BUY", "NO_TRADE")
                assert d.to_dict()["real_order_sent"] is False

    def test_insufficient_warmup(self) -> None:
        d = SessionVwapRangeLowVol().evaluate_with_context(
            self.flat_series(), 5, []
        )

        assert d.reason_code == "INSUFFICIENT_WARMUP"

    def test_deterministic(self) -> None:
        s1, s2 = SessionVwapRangeLowVol(), SessionVwapRangeLowVol()
        candles = self.trending_series()

        assert (s1.evaluate_with_context(candles, 100, []).to_dict()
                == s2.evaluate_with_context(candles, 100, []).to_dict())


class TestRejectedStrategiesStayRejected:

    def test_ema_has_no_v3_strategy_class(self) -> None:
        """
        EMA не получает новую версию: у неё нет gross edge (PF 0.92).
        Проверяем именно КЛАССЫ стратегий, а не любые имена — функция
        индикатора `ema` импортируется и это нормально.
        """
        import inspect

        import api.strategy_engine.strategies.v3_range_lowvol as v3
        from api.strategy_engine.strategies.contracts import BaseStrategy

        classes = [
            name for name, obj in vars(v3).items()
            if inspect.isclass(obj) and issubclass(obj, BaseStrategy)
        ]

        assert not any("EMA_STRUCTURE" in name.upper() for name in classes)
        assert "SessionVwapRangeLowVol" in classes

    def test_champion_unchanged(self) -> None:
        from api.strategy_supervisor import DEFAULT_STRATEGY_ID

        assert DEFAULT_STRATEGY_ID == "RANGE_NO_TRADE_POLICY"
