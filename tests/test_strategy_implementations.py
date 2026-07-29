"""
Тесты трёх реализованных стратегий.

Самый важный класс — TestNoFutureAccess. Утечка будущего делает результат
ЛУЧШЕ, а не хуже, поэтому она не выглядит как баг и обычно не находится.
Здесь она ловится структурно: CandleWindow физически не отдаёт свечи
правее текущей.

Второй по важности — adversarial-набор. Стратегия обязана отвечать
NO_TRADE с внятной причиной на мусорный вход, а не падать: падение в
цикле превращается в FAILED_SAFELY и останавливает наблюдение.
"""

from datetime import datetime, timezone

import pytest

from api.strategy_engine.strategies import (
    Candle,
    CandleWindow,
    LondonSessionBreakoutRetest,
    LookAheadError,
    SessionVwapTrendPullback,
    StrategyConfig,
    StrategyContractError,
    TrendPullbackEmaStructure,
    get_implementation,
)

ALL_STRATEGIES = (
    SessionVwapTrendPullback,
    LondonSessionBreakoutRetest,
    TrendPullbackEmaStructure,
)

FIVE_MIN_MS = 5 * 60 * 1000


def ts(day: int = 1, hour: int = 8, minute: int = 0) -> int:
    return int(
        datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc).timestamp()
        * 1000
    )


def candle(
    open_time_ms: int,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
) -> Candle:
    open_value = close if open_ is None else open_
    top = max(open_value, close) if high is None else high
    bottom = min(open_value, close) if low is None else low

    return Candle(
        open_time_ms=open_time_ms,
        open=open_value,
        high=top,
        low=bottom,
        close=close,
        volume=volume,
    )


def rising_series(count: int = 120, start_ms: int | None = None) -> list[Candle]:
    """Плавный аптренд с pullback'ами — материал для валидного лонга."""
    base = start_ms if start_ms is not None else ts(1, 0, 0)
    candles = []
    price = 100.0

    for index in range(count):
        # Каждая 7-я свеча — откат, остальные — рост.
        price = price - 0.6 if index % 7 == 6 else price + 1.0

        candles.append(
            candle(
                base + index * FIVE_MIN_MS,
                round(price, 2),
                open_=round(price - 0.4, 2),
                high=round(price + 0.5, 2),
                low=round(price - 0.9, 2),
            )
        )

    return candles


def flat_series(count: int = 120) -> list[Candle]:
    base = ts(1, 0, 0)

    return [
        candle(
            base + index * FIVE_MIN_MS,
            100.0,
            open_=100.0,
            high=100.05,
            low=99.95,
        )
        for index in range(count)
    ]


# =====================================================================
# Контракт окна: доступ к будущему невозможен
# =====================================================================


class TestNoFutureAccess:

    def test_window_refuses_candles_to_the_right(self) -> None:
        candles = rising_series(50)
        window = CandleWindow(candles, index=20)

        assert window[20] is candles[20]

        with pytest.raises(LookAheadError):
            window[21]

        with pytest.raises(LookAheadError):
            window[49]

    def test_len_reflects_available_history_only(self) -> None:
        window = CandleWindow(rising_series(50), index=20)

        assert len(window) == 21

    def test_negative_index_reads_backwards(self) -> None:
        candles = rising_series(50)
        window = CandleWindow(candles, index=20)

        assert window[-1] is candles[20]
        assert window[-2] is candles[19]

    def test_slice_and_closes_never_exceed_current(self) -> None:
        candles = rising_series(50)
        window = CandleWindow(candles, index=10)

        assert len(window.slice(100)) == 11
        assert len(window.closes(100)) == 11
        assert window.slice(100)[-1] is candles[10]

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_decision_is_identical_with_or_without_future_candles(
        self, strategy_class
    ) -> None:
        """
        Решающая проверка: добавление БУДУЩИХ свечей после текущего
        индекса не меняет решение. Если бы стратегия подглядывала, оно
        изменилось бы.
        """
        full = rising_series(200)
        index = 120

        strategy = strategy_class()

        with_future = strategy.evaluate_closed_candle(full, index)
        without_future = strategy.evaluate_closed_candle(full[: index + 1], index)

        assert with_future.to_dict() == without_future.to_dict()


# =====================================================================
# Детерминизм и общий контракт
# =====================================================================


class TestDeterminismAndContract:

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_repeated_evaluation_is_identical(self, strategy_class) -> None:
        candles = rising_series(150)
        strategy = strategy_class()

        first = strategy.evaluate_closed_candle(candles, 140)
        second = strategy.evaluate_closed_candle(candles, 140)

        assert first.to_dict() == second.to_dict()

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_fresh_instance_gives_same_result_restart_determinism(
        self, strategy_class
    ) -> None:
        """Рестарт процесса не должен менять решение по той же истории."""
        candles = rising_series(150)

        first = strategy_class().evaluate_closed_candle(candles, 140)
        second = strategy_class().evaluate_closed_candle(candles, 140)

        assert first.to_dict() == second.to_dict()

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_insufficient_warmup_is_refused(self, strategy_class) -> None:
        candles = rising_series(120)
        strategy = strategy_class()

        decision = strategy.evaluate_closed_candle(candles, 10)

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code == "INSUFFICIENT_WARMUP"
        assert decision.diagnostics["required"] == strategy.required_warmup_bars

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_never_emits_a_short(self, strategy_class) -> None:
        """LONG only: ни при каких данных не должно появиться SELL/SHORT."""
        for series in (rising_series(200), flat_series(200)):
            strategy = strategy_class()

            for index in range(60, len(series)):
                decision = strategy.evaluate_closed_candle(series, index)

                assert decision.signal in ("BUY", "NO_TRADE")
                assert decision.to_dict()["side"] in ("LONG", "NONE")
                assert decision.to_dict()["real_order_sent"] is False

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_no_trade_always_carries_a_reason(self, strategy_class) -> None:
        strategy = strategy_class()

        for index in range(60, 150):
            decision = strategy.evaluate_closed_candle(rising_series(200), index)

            if decision.signal == "NO_TRADE":
                assert decision.reason_code
                assert decision.reason_code != ""

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_trade_respects_minimum_risk_reward(self, strategy_class) -> None:
        strategy = strategy_class()

        for index in range(60, 200):
            decision = strategy.evaluate_closed_candle(rising_series(220), index)

            if decision.is_trade:
                assert decision.risk_reward >= strategy.config.min_risk_reward
                assert decision.stop < decision.entry
                assert decision.entry < decision.take_profit_1
                assert decision.take_profit_1 < decision.take_profit_2

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_config_is_immutable(self, strategy_class) -> None:
        import dataclasses

        strategy = strategy_class()

        with pytest.raises(dataclasses.FrozenInstanceError):
            strategy.config.min_risk_reward = 1.0  # type: ignore[misc]

    def test_config_fingerprint_changes_with_parameters(self) -> None:
        """Отпечаток обязан отличать другие параметры — иначе он бесполезен."""
        base = StrategyConfig()
        changed = StrategyConfig(fast_ema=21)

        assert base.fingerprint() != changed.fingerprint()
        assert base.fingerprint() == StrategyConfig().fingerprint()

    def test_registry_lookup_returns_none_for_unimplemented(self) -> None:
        assert get_implementation("SESSION_VWAP_TREND_PULLBACK") is not None
        # ORB живёт в другом пакете, политика RANGE не является стратегией.
        assert get_implementation("ORB_0930_RETEST") is None
        assert get_implementation("RANGE_NO_TRADE_POLICY") is None
        assert get_implementation("INVENTED") is None


# =====================================================================
# Условия отказа, специфичные для стратегий
# =====================================================================


class TestSessionVwapConditions:

    def test_flat_market_is_refused_as_range(self) -> None:
        decision = SessionVwapTrendPullback().evaluate_closed_candle(
            flat_series(120), 110
        )

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code == "RANGE_REGIME_ATR_TOO_LOW"

    def test_downtrend_is_refused(self) -> None:
        base = ts(1, 0, 0)
        falling = [
            candle(base + i * FIVE_MIN_MS, round(200.0 - i * 1.0, 2))
            for i in range(120)
        ]

        decision = SessionVwapTrendPullback().evaluate_closed_candle(falling, 110)

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code in (
            "TREND_NOT_UP",
            "PRICE_BELOW_VWAP",
            "VOLATILITY_TOO_HIGH",
            "RANGE_REGIME_ATR_TOO_LOW",
        )

    def test_zero_volume_session_cannot_produce_vwap(self) -> None:
        """VWAP без объёма не определён; подставлять close нельзя."""
        base = ts(1, 0, 0)
        series = [
            candle(base + i * FIVE_MIN_MS, round(100.0 + i * 0.9, 2), volume=0.0)
            for i in range(120)
        ]

        decision = SessionVwapTrendPullback().evaluate_closed_candle(series, 110)

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code in (
            "VWAP_UNAVAILABLE",
            "VOLATILITY_TOO_HIGH",
            "RANGE_REGIME_ATR_TOO_LOW",
        )


class TestLondonSessionConditions:

    def test_candle_outside_session_is_refused(self) -> None:
        """03:00 UTC — вне London 07:00-16:00."""
        series = rising_series(120, start_ms=ts(1, 0, 0))

        strategy = LondonSessionBreakoutRetest()
        decision = strategy.evaluate_closed_candle(series, 30)

        if decision.reason_code != "INSUFFICIENT_WARMUP":
            assert decision.signal == "NO_TRADE"

    def test_session_boundaries_use_project_schedule(self) -> None:
        """Границы обязаны совпадать с config/trading_sessions.py."""
        from api.strategy_engine.strategies.london_session_breakout_retest import (
            LONDON_END_MINUTES,
            LONDON_START_MINUTES,
        )
        from config.trading_sessions import TRADING_SESSIONS

        london = TRADING_SESSIONS["LONDON"]
        start_hour, start_minute = map(int, london["start"].split(":"))
        end_hour, end_minute = map(int, london["end"].split(":"))

        assert LONDON_START_MINUTES == start_hour * 60 + start_minute
        assert LONDON_END_MINUTES == end_hour * 60 + end_minute

    def test_no_entry_without_breakout(self) -> None:
        """Плоская сессия: пробоя нет — входа быть не может."""
        series = flat_series(200)

        strategy = LondonSessionBreakoutRetest()

        for index in range(60, 200):
            decision = strategy.evaluate_closed_candle(series, index)
            assert decision.signal == "NO_TRADE"

    def test_chase_entry_without_retest_is_refused(self) -> None:
        """
        Резкий пробой без возврата к границе не должен давать вход:
        запрет chase-entry.
        """
        base = ts(1, 6, 0)
        series = []

        # 12 свечей до сессии + диапазон + вертикальный уход вверх.
        for index in range(100):
            price = 100.0 if index < 80 else 100.0 + (index - 80) * 5.0

            series.append(
                candle(base + index * FIVE_MIN_MS, round(price, 2))
            )

        strategy = LondonSessionBreakoutRetest()

        for index in range(60, 100):
            decision = strategy.evaluate_closed_candle(series, index)

            # Вход допустим ТОЛЬКО с кодом retest.
            if decision.is_trade:
                assert decision.reason_code == "LONDON_BREAKOUT_RETEST_CONFIRMED"
                assert decision.diagnostics.get("range_high") is not None


class TestTrendPullbackConditions:

    def test_ema_not_aligned_is_refused(self) -> None:
        base = ts(1, 0, 0)
        falling = [
            candle(base + i * FIVE_MIN_MS, round(200.0 - i * 0.8, 2))
            for i in range(120)
        ]

        decision = TrendPullbackEmaStructure().evaluate_closed_candle(falling, 110)

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code == "EMA_NOT_ALIGNED"

    def test_flat_market_gives_no_structure(self) -> None:
        decision = TrendPullbackEmaStructure().evaluate_closed_candle(
            flat_series(120), 110
        )

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code in (
            "EMA_NOT_ALIGNED",
            "STRUCTURE_UNCONFIRMED",
            "ATR_UNAVAILABLE",
        )


# =====================================================================
# Adversarial
# =====================================================================


class TestAdversarialInputs:

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_spike_candle_does_not_crash(self, strategy_class) -> None:
        series = rising_series(150)
        spike = series[100]

        series[100] = Candle(
            open_time_ms=spike.open_time_ms,
            open=spike.open,
            high=spike.high * 50,
            low=spike.low,
            close=spike.close,
            volume=spike.volume,
        )

        decision = strategy_class().evaluate_closed_candle(series, 140)

        assert decision.signal in ("BUY", "NO_TRADE")

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_price_gap_does_not_crash(self, strategy_class) -> None:
        series = rising_series(150)

        for index in range(100, 150):
            old = series[index]
            series[index] = Candle(
                open_time_ms=old.open_time_ms,
                open=old.open * 3,
                high=old.high * 3,
                low=old.low * 3,
                close=old.close * 3,
                volume=old.volume,
            )

        decision = strategy_class().evaluate_closed_candle(series, 140)

        assert decision.signal in ("BUY", "NO_TRADE")

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_zero_volume_does_not_crash(self, strategy_class) -> None:
        series = [
            Candle(
                open_time_ms=item.open_time_ms,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=0.0,
            )
            for item in rising_series(150)
        ]

        decision = strategy_class().evaluate_closed_candle(series, 140)

        assert decision.signal in ("BUY", "NO_TRADE")

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_duplicate_candles_do_not_crash(self, strategy_class) -> None:
        series = rising_series(150)
        duplicated = series[:100] + [series[99]] + series[100:]

        decision = strategy_class().evaluate_closed_candle(duplicated, 140)

        assert decision.signal in ("BUY", "NO_TRADE")

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_missing_candles_do_not_crash(self, strategy_class) -> None:
        """Пропуск во времени: свечи 50..70 отсутствуют."""
        series = rising_series(150)
        gapped = series[:50] + series[70:]

        decision = strategy_class().evaluate_closed_candle(gapped, len(gapped) - 1)

        assert decision.signal in ("BUY", "NO_TRADE")

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_extreme_volatility_does_not_crash(self, strategy_class) -> None:
        base = ts(1, 0, 0)
        series = []
        price = 100.0

        for index in range(150):
            price = price * (1.4 if index % 2 == 0 else 0.72)
            price = max(price, 1.0)

            series.append(candle(base + index * FIVE_MIN_MS, round(price, 2)))

        decision = strategy_class().evaluate_closed_candle(series, 140)

        assert decision.signal in ("BUY", "NO_TRADE")

    @pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
    def test_index_outside_range_is_refused(self, strategy_class) -> None:
        series = rising_series(100)

        with pytest.raises(StrategyContractError):
            strategy_class().evaluate_closed_candle(series, 500)

        with pytest.raises(StrategyContractError):
            strategy_class().evaluate_closed_candle(series, -1)


class TestMalformedCandles:

    def test_negative_price_is_refused(self) -> None:
        with pytest.raises(StrategyContractError):
            Candle(open_time_ms=1, open=-1.0, high=1.0, low=-2.0, close=0.5,
                   volume=1.0)

    def test_low_above_high_is_refused(self) -> None:
        with pytest.raises(StrategyContractError):
            Candle(open_time_ms=1, open=5.0, high=4.0, low=6.0, close=5.0,
                   volume=1.0)

    def test_close_outside_range_is_refused(self) -> None:
        with pytest.raises(StrategyContractError):
            Candle(open_time_ms=1, open=5.0, high=6.0, low=4.0, close=9.0,
                   volume=1.0)

    def test_nan_is_refused(self) -> None:
        with pytest.raises(StrategyContractError):
            Candle(open_time_ms=1, open=float("nan"), high=6.0, low=4.0,
                   close=5.0, volume=1.0)

    def test_negative_volume_is_refused(self) -> None:
        with pytest.raises(StrategyContractError):
            Candle(open_time_ms=1, open=5.0, high=6.0, low=4.0, close=5.0,
                   volume=-1.0)

    def test_malformed_rows_are_dropped_not_repaired(self) -> None:
        """Молча починенная свеча — это выдуманные данные."""
        from api.strategy_engine.strategies import candles_from_arrays

        candles = candles_from_arrays(
            {
                "timestamps": [1000, 2000, 3000],
                "opens": [10.0, -5.0, 12.0],
                "highs": [11.0, 6.0, 13.0],
                "lows": [9.0, 4.0, 11.0],
                "closes": [10.5, 5.0, 12.5],
                "volumes": [1.0, 1.0, 1.0],
            }
        )

        assert len(candles) == 2

    def test_out_of_order_input_is_sorted(self) -> None:
        from api.strategy_engine.strategies import candles_from_arrays

        candles = candles_from_arrays(
            {
                "timestamps": [3000, 1000, 2000],
                "opens": [12.0, 10.0, 11.0],
                "highs": [13.0, 11.0, 12.0],
                "lows": [11.0, 9.0, 10.0],
                "closes": [12.5, 10.5, 11.5],
                "volumes": [1.0, 1.0, 1.0],
            }
        )

        assert [item.open_time_ms for item in candles] == [1000, 2000, 3000]
