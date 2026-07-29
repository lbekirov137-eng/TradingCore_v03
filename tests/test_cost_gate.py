"""
Тесты CostViabilityGate и аудита издержек.

Главное проверяемое свойство — тождество cost_r = cost_rate / stop_percent.
Из него следует, что издержки в R НЕ зависят ни от риска, ни от размера
счёта, а только от ширины стопа. Именно поэтому «снизить риск» не лечит
проблему, а «расширить стоп» лечит линейно.
"""

import pytest

from api.paper_trading.cost_audit import (
    MAKER_TAKER,
    TAKER_TAKER,
    CostScenario,
    aggregate_candles,
    evaluate_scenario,
    execution_rates,
)
from api.paper_trading.cost_model import TradingCostConfig
from api.strategy_engine.cost_gate import (
    COST_TOO_HIGH,
    COST_VIABLE,
    MIN_NOTIONAL_CONFLICT,
    NET_RR_TOO_LOW,
    SLIPPAGE_RISK_TOO_HIGH,
    STOP_TOO_TIGHT_FOR_COSTS,
    CostGateConfig,
    evaluate_cost_viability,
    required_stop_percent,
)
from api.strategy_engine.strategies.contracts import Candle


CONFIG = TradingCostConfig()


class TestCostIdentity:

    def test_cost_r_depends_only_on_stop_percent(self) -> None:
        """
        Ключевое тождество. Одинаковый процентный стоп даёт одинаковые
        издержки в R при ЛЮБОМ риске — поэтому снижение риска бесполезно.
        """
        results = []

        for risk in (0.5, 1.0, 10.0, 1000.0):
            scenario = CostScenario(
                "X", "15m", "test", 0.28, TAKER_TAKER
            )
            results.append(
                evaluate_scenario(scenario, 68000.0, risk, CONFIG)["cost_r"]
            )

        assert len(set(results)) == 1, "cost_r must not depend on risk size"

    def test_wider_stop_reduces_cost_r_linearly(self) -> None:
        narrow = evaluate_scenario(
            CostScenario("N", "15m", "1x", 0.28, TAKER_TAKER),
            68000.0, 1.0, CONFIG,
        )["cost_r"]

        wide = evaluate_scenario(
            CostScenario("W", "15m", "2x", 0.56, TAKER_TAKER),
            68000.0, 1.0, CONFIG,
        )["cost_r"]

        # abs=1e-4, потому что cost_r округляется до 4 знаков при выдаче:
        # точное равенство здесь проверяло бы округление, а не линейность.
        assert wide == pytest.approx(narrow / 2, abs=1e-4)

    def test_five_minute_one_atr_is_around_two_r(self) -> None:
        """Воспроизводит наблюдённую катастрофу 5m / 1 ATR."""
        result = evaluate_scenario(
            CostScenario("A", "5m", "1.0 ATR", 0.1462, TAKER_TAKER),
            68808.0, 1.0, CONFIG,
        )

        assert 2.0 < result["cost_r"] < 2.3
        # При gross 1:2 безубыток недостижим ни при какой доле побед.
        assert result["required_win_rate_rr2"] is None

    def test_maker_entry_is_cheaper_than_taker(self) -> None:
        taker = execution_rates(TAKER_TAKER, CONFIG)["round_trip_rate"]
        maker = execution_rates(MAKER_TAKER, CONFIG)["round_trip_rate"]

        assert maker < taker
        # Экономия — только проскальзывание и половина спреда на входе:
        # комиссии maker и taker в конфиге равны.
        assert taker - maker == pytest.approx(
            CONFIG.slippage_rate + 0.5 / 10_000.0, rel=1e-9
        )

    def test_required_stop_percent_inverts_the_identity(self) -> None:
        needed = required_stop_percent(0.25, TAKER_TAKER, CONFIG)

        achieved = evaluate_scenario(
            CostScenario("R", "x", "x", needed, TAKER_TAKER),
            68000.0, 1.0, CONFIG,
        )["cost_r"]

        assert achieved == pytest.approx(0.25, rel=1e-6)


class TestGateRefusals:

    def base(self, **overrides):
        params = {
            "entry": 68000.0,
            "stop": 67000.0,
            "take_profit": 70000.0,
            "risk_amount": 1.0,
        }
        params.update(overrides)
        return params

    def test_viable_trade_passes(self) -> None:
        """Широкий стоп (~1.5% цены) и gross 1:2 проходят гейт."""
        result = evaluate_cost_viability(
            entry=68000.0,
            stop=67000.0,
            take_profit=70200.0,
            risk_amount=1.0,
        )

        assert result["viable"] is True
        assert result["reason_code"] == COST_VIABLE
        assert result["estimated_cost_r"] <= 0.25

    def test_tight_stop_is_refused_as_too_costly(self) -> None:
        """5m-подобный стоп 0.15% цены."""
        result = evaluate_cost_viability(
            entry=68000.0,
            stop=68000.0 * (1 - 0.0015),
            take_profit=68000.0 * (1 + 0.003),
            risk_amount=1.0,
        )

        assert result["viable"] is False
        assert result["reason_code"] in (
            COST_TOO_HIGH,
            SLIPPAGE_RISK_TOO_HIGH,
            STOP_TOO_TIGHT_FOR_COSTS,
        )
        assert result["estimated_cost_r"] > 0.25

    def test_net_rr_too_low_is_refused(self) -> None:
        """Стоп широкий (издержки малы), но цель слишком близко."""
        result = evaluate_cost_viability(
            entry=68000.0,
            stop=66000.0,
            take_profit=68500.0,
            risk_amount=1.0,
        )

        assert result["viable"] is False
        assert result["reason_code"] == NET_RR_TOO_LOW
        assert result["net_rr_after_costs"] < 1.5

    def test_min_notional_conflict_is_detected(self) -> None:
        """Очень широкий стоп -> крошечный номинал."""
        result = evaluate_cost_viability(
            entry=68000.0,
            stop=1000.0,
            take_profit=200000.0,
            risk_amount=1.0,
        )

        assert result["viable"] is False
        assert result["reason_code"] == MIN_NOTIONAL_CONFLICT

    def test_slippage_risk_is_detected(self) -> None:
        config = CostGateConfig(max_slippage_to_stop_ratio=0.001)

        result = evaluate_cost_viability(
            entry=68000.0,
            stop=67000.0,
            take_profit=70200.0,
            risk_amount=1.0,
            config=config,
        )

        assert result["viable"] is False
        assert result["reason_code"] == SLIPPAGE_RISK_TOO_HIGH

    def test_stop_too_tight_for_spread_is_detected(self) -> None:
        config = CostGateConfig(min_stop_to_spread_ratio=1e9)

        result = evaluate_cost_viability(
            entry=68000.0,
            stop=67000.0,
            take_profit=70200.0,
            risk_amount=1.0,
            config=config,
        )

        assert result["viable"] is False
        assert result["reason_code"] == STOP_TOO_TIGHT_FOR_COSTS

    def test_inverted_levels_are_refused(self) -> None:
        result = evaluate_cost_viability(
            entry=68000.0,
            stop=69000.0,
            take_profit=70000.0,
            risk_amount=1.0,
        )

        assert result["viable"] is False
        assert result["reason_code"] == NET_RR_TOO_LOW

    def test_refusal_always_carries_the_numbers(self) -> None:
        """Отказ без чисел невозможно проверить."""
        result = evaluate_cost_viability(
            entry=68000.0,
            stop=68000.0 * (1 - 0.0015),
            take_profit=68000.0 * (1 + 0.003),
            risk_amount=1.0,
        )

        for field in (
            "estimated_round_trip_cost",
            "estimated_cost_r",
            "net_reward_r",
            "net_rr_after_costs",
        ):
            assert field in result, field

    def test_gate_config_is_immutable(self) -> None:
        import dataclasses

        config = CostGateConfig()

        with pytest.raises(dataclasses.FrozenInstanceError):
            config.max_cost_r = 5.0  # type: ignore[misc]


class TestAggregation:

    def make(self, count: int) -> list[Candle]:
        return [
            Candle(
                open_time_ms=i * 300_000,
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=10.0,
            )
            for i in range(count)
        ]

    def test_three_five_minute_bars_make_one_fifteen(self) -> None:
        aggregated = aggregate_candles(self.make(9), 3)

        assert len(aggregated) == 3
        assert aggregated[0].open == 100.0
        assert aggregated[0].close == 102.5
        assert aggregated[0].high == 103.0
        assert aggregated[0].low == 99.0
        assert aggregated[0].volume == 30.0

    def test_aggregation_does_not_overlap(self) -> None:
        """
        Скользящее окно дало бы коррелированные бары и завысило бы число
        независимых наблюдений.
        """
        aggregated = aggregate_candles(self.make(12), 3)

        starts = [item.open_time_ms for item in aggregated]

        assert starts == [0, 900_000, 1_800_000, 2_700_000]

    def test_incomplete_tail_block_is_dropped(self) -> None:
        """Неполный бар — это ещё не закрытая свеча."""
        assert len(aggregate_candles(self.make(10), 3)) == 3
