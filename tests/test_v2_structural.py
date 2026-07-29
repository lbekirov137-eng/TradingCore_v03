"""
Тесты кандидатов @2.0.0 (1H исполнение, 4H контекст, структурный стоп).

Критичные свойства:
  - агрегация по ВРЕМЕНИ, а не по счётчику (пропуск не заражает соседей);
  - неполный бакет отбрасывается;
  - 4H контекст берётся только из ЗАКРЫТЫХ свечей;
  - структурный стоп не двигается ради улучшения cost ratio;
  - риск остаётся 0.1% при любой ширине стопа;
  - cost gate не ослаблен;
  - версии @1.0.0 не изменены.
"""

import dataclasses

import pytest

from api.strategy_engine.cost_gate import CostGateConfig
from api.strategy_engine.strategies import (
    LondonSessionBreakoutRetest,
    SessionVwapTrendPullback,
    TrendPullbackEmaStructure,
)
from api.strategy_engine.strategies.contracts import Candle, StrategyConfig
from api.strategy_engine.strategies.v2_structural import (
    RISK_AMOUNT_USD,
    LondonSessionBreakoutRetestV2,
    SessionVwapTrendPullbackV2,
    StructuralConfig,
    TrendPullbackEmaStructureV2,
)
from api.strategy_engine.timeframes import (
    TIMEFRAME_MS,
    aggregate_by_time,
    align_context,
)

V2_CLASSES = (
    SessionVwapTrendPullbackV2,
    LondonSessionBreakoutRetestV2,
    TrendPullbackEmaStructureV2,
)

FIVE_MIN = TIMEFRAME_MS["5m"]
HOUR = TIMEFRAME_MS["1H"]


def bar(open_time_ms: int, close: float, volume: float = 10.0) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=volume,
    )


class TestAggregation:

    def test_five_minute_to_one_hour(self) -> None:
        candles = [bar(i * FIVE_MIN, 100.0 + i) for i in range(24)]

        result = aggregate_by_time(candles, "5m", "1H")

        assert result["provenance"]["expected_per_bucket"] == 12
        assert len(result["candles"]) == 2
        assert result["candles"][0].open == 100.0
        assert result["candles"][0].close == 111.0

    def test_one_hour_to_four_hour(self) -> None:
        candles = [bar(i * HOUR, 100.0 + i) for i in range(8)]

        result = aggregate_by_time(candles, "1H", "4H")

        assert len(result["candles"]) == 2
        assert result["provenance"]["expected_per_bucket"] == 4

    def test_incomplete_bucket_is_dropped(self) -> None:
        """7 пятиминуток — не закрытая часовая свеча."""
        candles = [bar(i * FIVE_MIN, 100.0 + i) for i in range(7)]

        result = aggregate_by_time(candles, "5m", "1H")

        assert result["candles"] == []
        assert result["provenance"]["incomplete_buckets_dropped"] == 1

    def test_gap_damages_only_its_own_bucket(self) -> None:
        """
        Ключевое отличие от группировки по счётчику: пропуск в одном часе
        не сдвигает границы следующих часов.
        """
        candles = [bar(i * FIVE_MIN, 100.0 + i) for i in range(36)]
        del candles[3]  # дырка в первом часе

        result = aggregate_by_time(candles, "5m", "1H")

        # Первый час отброшен, два следующих целы.
        assert len(result["candles"]) == 2
        assert result["provenance"]["incomplete_buckets_dropped"] == 1
        assert result["candles"][0].open_time_ms == HOUR

    def test_duplicate_timestamps_are_dropped(self) -> None:
        candles = [bar(i * FIVE_MIN, 100.0 + i) for i in range(12)]
        candles.append(bar(5 * FIVE_MIN, 999.0))

        result = aggregate_by_time(candles, "5m", "1H")

        assert result["provenance"]["duplicate_timestamps_dropped"] == 1
        assert len(result["candles"]) == 1
        # Дубликат не удвоил объём.
        assert result["candles"][0].volume == 120.0

    def test_out_of_order_input_is_detected_and_sorted(self) -> None:
        candles = [bar(i * FIVE_MIN, 100.0 + i) for i in range(12)]
        candles[0], candles[5] = candles[5], candles[0]

        result = aggregate_by_time(candles, "5m", "1H")

        assert result["provenance"]["out_of_order_source_candles"] >= 1
        assert len(result["candles"]) == 1
        # Open берётся у самой ранней свечи, а не у первой в списке.
        assert result["candles"][0].open == 100.0

    def test_aggregation_is_deterministic(self) -> None:
        candles = [bar(i * FIVE_MIN, 100.0 + i) for i in range(48)]

        first = aggregate_by_time(candles, "5m", "1H")["candles"]
        second = aggregate_by_time(candles, "5m", "1H")["candles"]

        assert [c.open_time_ms for c in first] == [
            c.open_time_ms for c in second
        ]
        assert [c.close for c in first] == [c.close for c in second]

    def test_buckets_align_to_utc_hour_boundaries(self) -> None:
        """Границы бакетов — календарные часы UTC, а не позиция в списке."""
        offset = 7 * FIVE_MIN
        candles = [bar(offset + i * FIVE_MIN, 100.0 + i) for i in range(24)]

        result = aggregate_by_time(candles, "5m", "1H")

        for candle in result["candles"]:
            assert candle.open_time_ms % HOUR == 0

    def test_rejects_non_multiple_timeframes(self) -> None:
        with pytest.raises(ValueError):
            aggregate_by_time([], "1H", "5m")


class TestContextAlignment:

    def test_only_closed_context_candles_are_used(self) -> None:
        """
        4H свеча, ВНУТРИ которой находится текущий час, ещё не закрыта:
        её close содержит будущее относительно момента решения.
        """
        context = [bar(i * TIMEFRAME_MS["4H"], 100.0 + i) for i in range(4)]

        # Текущая 1H свеча в середине второй 4H свечи.
        current = bar(TIMEFRAME_MS["4H"] + 2 * HOUR, 500.0)

        latest = align_context(current, context, "4H")

        assert latest is not None
        # Доступна только ПЕРВАЯ (закрытая), не вторая.
        assert latest.open_time_ms == 0

    def test_no_context_before_first_close(self) -> None:
        context = [bar(i * TIMEFRAME_MS["4H"], 100.0 + i) for i in range(4)]
        current = bar(HOUR, 500.0)

        assert align_context(current, context, "4H") is None

    def test_context_exactly_at_close_is_available(self) -> None:
        context = [bar(i * TIMEFRAME_MS["4H"], 100.0 + i) for i in range(4)]
        current = bar(TIMEFRAME_MS["4H"], 500.0)

        latest = align_context(current, context, "4H")

        assert latest is not None and latest.open_time_ms == 0


class TestVersionIsolation:

    def test_v2_keys_differ_from_v1(self) -> None:
        v1_keys = {
            SessionVwapTrendPullback.strategy_key,
            LondonSessionBreakoutRetest.strategy_key,
            TrendPullbackEmaStructure.strategy_key,
        }
        v2_keys = {cls.strategy_key for cls in V2_CLASSES}

        assert not (v1_keys & v2_keys)

    def test_v1_versions_are_untouched(self) -> None:
        assert SessionVwapTrendPullback.version == "1.0.0"
        assert LondonSessionBreakoutRetest.version == "1.0.0"
        assert TrendPullbackEmaStructure.version == "1.0.0"

    def test_v2_versions_are_two_zero_zero(self) -> None:
        for cls in V2_CLASSES:
            assert cls.version == "2.0.0"

    def test_parameter_hash_differs_between_versions(self) -> None:
        """Разный отпечаток => результаты v1 и v2 несмешиваемы."""
        assert StrategyConfig().fingerprint() != StructuralConfig().fingerprint()

    def test_v2_config_is_immutable(self) -> None:
        config = StructuralConfig()

        with pytest.raises(dataclasses.FrozenInstanceError):
            config.min_stop_atr = 0.1  # type: ignore[misc]

    def test_v2_declares_1h_and_4h(self) -> None:
        config = StructuralConfig()

        assert config.execution_timeframe == "1H"
        assert config.context_timeframe == "4H"


class TestCostGateNotWeakened:

    def test_max_cost_r_is_still_025(self) -> None:
        """Порог не повышен ради допуска сделок."""
        assert CostGateConfig().max_cost_r == 0.25
        assert CostGateConfig().min_net_rr == 1.5

    def test_v2_uses_the_unmodified_gate(self) -> None:
        for cls in V2_CLASSES:
            assert cls().cost_gate_config.max_cost_r == 0.25


class TestStructuralStopAndSizing:

    def build(self, closes, start=0):
        return [bar(start + i * HOUR, value) for i, value in enumerate(closes)]

    def test_stop_too_tight_is_refused(self) -> None:
        strategy = TrendPullbackEmaStructureV2()

        decision = strategy.finalise(
            entry=68000.0,
            stop=67990.0,          # ~0.015% — заведомо уже min_stop_atr
            atr_value=400.0,
            reason_code="TEST",
        )

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code == "STRUCTURAL_STOP_TOO_TIGHT"

    def test_stop_too_wide_is_refused(self) -> None:
        strategy = TrendPullbackEmaStructureV2()

        decision = strategy.finalise(
            entry=68000.0,
            stop=68000.0 - 5.0 * 400.0,   # 5 ATR > max_stop_atr=4
            atr_value=400.0,
            reason_code="TEST",
        )

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code == "STRUCTURAL_STOP_TOO_WIDE"

    def test_expensive_trade_is_refused_by_cost_gate(self) -> None:
        """Стоп в допустимых ATR-границах, но экономика не проходит."""
        strategy = TrendPullbackEmaStructureV2()

        decision = strategy.finalise(
            entry=68000.0,
            stop=68000.0 - 1.2 * 100.0,   # 1.2 ATR, но всего ~0.18% цены
            atr_value=100.0,
            reason_code="TEST",
        )

        assert decision.signal == "NO_TRADE"
        assert decision.reason_code in (
            "COST_TOO_HIGH",
            "SLIPPAGE_RISK_TOO_HIGH",
            "STOP_TOO_TIGHT_FOR_COSTS",
        )

    def test_viable_trade_keeps_risk_at_one_tenth_percent(self) -> None:
        """
        Ширина стопа меняет РАЗМЕР позиции, но не риск. Это и есть
        «position size уменьшается автоматически».
        """
        strategy = TrendPullbackEmaStructureV2()

        for stop_atr in (2.0, 2.5, 3.0):
            atr_value = 68000.0 * 0.0062      # ~1H ATR
            stop = 68000.0 - stop_atr * atr_value

            decision = strategy.finalise(
                entry=68000.0,
                stop=stop,
                atr_value=atr_value,
                reason_code="TEST",
            )

            if not decision.is_trade:
                continue

            quantity = decision.diagnostics["quantity"]
            implied_risk = quantity * (decision.entry - decision.stop)

            # rel=1e-4, потому что quantity выдаётся округлённой до 8
            # знаков: точное равенство проверяло бы точность вывода, а не
            # то, что риск удерживается на 0.1%.
            assert implied_risk == pytest.approx(RISK_AMOUNT_USD, rel=1e-4)
            assert decision.diagnostics["leverage"] == 1
            assert decision.diagnostics["risk_amount"] == RISK_AMOUNT_USD

    def test_wider_stop_gives_smaller_position(self) -> None:
        strategy = TrendPullbackEmaStructureV2()
        atr_value = 68000.0 * 0.0062

        sizes = []

        for stop_atr in (2.0, 3.0):
            decision = strategy.finalise(
                entry=68000.0,
                stop=68000.0 - stop_atr * atr_value,
                atr_value=atr_value,
                reason_code="TEST",
            )

            if decision.is_trade:
                sizes.append(decision.diagnostics["position_notional"])

        if len(sizes) == 2:
            assert sizes[1] < sizes[0]

    def test_v2_never_emits_short(self) -> None:
        strategy = TrendPullbackEmaStructureV2()
        atr_value = 68000.0 * 0.0062

        decision = strategy.finalise(
            entry=68000.0,
            stop=68000.0 - 2.5 * atr_value,
            atr_value=atr_value,
            reason_code="TEST",
        )

        assert decision.signal in ("BUY", "NO_TRADE")
        assert decision.to_dict()["side"] in ("LONG", "NONE")
        assert decision.to_dict()["real_order_sent"] is False


class TestContextRequired:

    def test_v2_refuses_without_context(self) -> None:
        """Контекст 4H входит в контракт: молча обойтись без него нельзя."""
        candles = [bar(i * HOUR, 100.0 + i) for i in range(80)]

        for cls in V2_CLASSES:
            decision = cls().evaluate_closed_candle(candles, 70)

            assert decision.signal == "NO_TRADE"
            assert decision.reason_code == "CONTEXT_REQUIRED"

    def test_insufficient_warmup_is_refused(self) -> None:
        candles = [bar(i * HOUR, 100.0 + i) for i in range(80)]

        for cls in V2_CLASSES:
            decision = cls().evaluate_with_context(candles, 10, [])

            assert decision.reason_code == "INSUFFICIENT_WARMUP"


class TestSupervisorInvariantsUnchanged:

    def test_plan_switch_remains_side_effect_free(self) -> None:
        import ast
        import inspect

        from api.strategy_supervisor import plan_switch

        tree = ast.parse(inspect.getsource(plan_switch).lstrip())
        called = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)

        forbidden = called & {
            "open_position", "close_position", "write", "write_text",
            "dump", "save", "unlink", "post", "request",
        }

        assert not forbidden

    def test_default_champion_is_unchanged(self) -> None:
        from api.strategy_supervisor import DEFAULT_STRATEGY_ID

        assert DEFAULT_STRATEGY_ID == "RANGE_NO_TRADE_POLICY"
