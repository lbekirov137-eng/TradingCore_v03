"""
Модель комиссий и проскальзывания.

До неё paper-результат был систематически завышен: сделка «зарабатывала»
полную разницу цен, тогда как на бирже её уменьшили бы комиссия за вход,
комиссия за выход и проскальзывание. Для цели 3R при риске 0.1% капитала
это искажение сопоставимо с самим результатом.

Тесты фиксируют: gross и net разделены, net всегда хуже gross при
ненулевых издержках, конфигурация применяется и попадает в журнал.
"""

from pathlib import Path

import pytest

from api.paper_trading.cost_model import (
    LONG,
    MAKER,
    TAKER,
    TradingCostConfig,
    apply_slippage,
    compute_trade_costs,
)
from api.paper_trading.position_manager import PaperPositionManager


def build_paper_order() -> dict:
    return {
        "mode": "PAPER",
        "status": "FILLED_SIMULATED",
        "real_order_sent": False,
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "signal": "BUY",
        "side": "LONG",
        "entry": 100.0,
        "quantity": 0.1,
        "stop": 90.0,
        "take_profit_1": 120.0,
        "take_profit_2": 130.0,
        "risk_amount": 1.0,
        "execution_mode": "SPOT_LONG_ONLY",
    }


class TestZeroCostModel:

    def test_zero_config_leaves_pnl_untouched(self):
        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=130.0,
            quantity=0.1,
            config=TradingCostConfig.zero_cost(),
        )

        assert result["gross_pnl"] == pytest.approx(3.0)
        assert result["net_pnl"] == pytest.approx(3.0)
        assert result["total_fees"] == 0.0
        assert result["slippage_cost"] == 0.0
        assert result["entry_price_effective"] == 100.0
        assert result["exit_price_effective"] == 130.0

    def test_zero_cost_must_be_explicit(self):
        """Нулевые издержки нельзя получить по умолчанию."""
        default = TradingCostConfig()

        assert default.taker_fee_rate > 0
        assert default.maker_fee_rate > 0
        assert default.slippage_bps > 0
        assert default.slippage_enabled is True


class TestFeesOnBothSides:

    def test_entry_and_exit_fees_are_charged_separately(self):
        config = TradingCostConfig(
            maker_fee_rate=0.0,
            taker_fee_rate=0.001,
            slippage_bps=0.0,
            slippage_enabled=False,
        )

        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=130.0,
            quantity=0.1,
            config=config,
        )

        # Комиссия берётся с оборота каждой стороны отдельно.
        assert result["entry_fee"] == pytest.approx(100.0 * 0.1 * 0.001)
        assert result["exit_fee"] == pytest.approx(130.0 * 0.1 * 0.001)
        assert result["total_fees"] == pytest.approx(
            result["entry_fee"] + result["exit_fee"]
        )
        assert result["entry_fee"] != result["exit_fee"]

    def test_maker_and_taker_rates_differ(self):
        config = TradingCostConfig(
            maker_fee_rate=0.0002,
            taker_fee_rate=0.001,
            slippage_bps=0.0,
            slippage_enabled=False,
            entry_liquidity=MAKER,
            exit_liquidity=TAKER,
        )

        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=100.0,
            quantity=1.0,
            config=config,
        )

        assert result["entry_fee"] == pytest.approx(100.0 * 0.0002)
        assert result["exit_fee"] == pytest.approx(100.0 * 0.001)


class TestGrossVersusNet:

    def test_winning_trade_net_is_worse_than_gross(self):
        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=130.0,
            quantity=0.1,
        )

        assert result["gross_pnl"] == pytest.approx(3.0)
        assert result["net_pnl"] < result["gross_pnl"]

    def test_losing_trade_net_is_worse_than_gross(self):
        """Издержки увеличивают убыток, а не уменьшают его."""
        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=90.0,
            quantity=0.1,
        )

        assert result["gross_pnl"] == pytest.approx(-1.0)
        assert result["net_pnl"] < result["gross_pnl"]

    def test_net_equals_gross_minus_all_costs(self):
        """Инвариант разложения: net = gross - slippage - fees."""
        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=130.0,
            quantity=0.1,
        )

        assert result["net_pnl"] == pytest.approx(
            result["gross_pnl"]
            - result["slippage_cost"]
            - result["total_fees"],
            abs=1e-8,
        )

    def test_costs_can_turn_a_small_win_into_a_loss(self):
        """
        Главное практическое следствие: сделка с крошечным плюсом на
        самом деле убыточна. Без модели издержек такие сделки
        засчитывались бы как прибыльные.
        """
        result = compute_trade_costs(
            entry_price=100.0,
            exit_price=100.05,
            quantity=1.0,
        )

        assert result["gross_pnl"] > 0
        assert result["net_pnl"] < 0


class TestSlippageAlwaysHurts:

    def test_long_entry_is_filled_higher(self):
        config = TradingCostConfig(slippage_bps=10.0)

        assert apply_slippage(
            100.0, side=LONG, is_entry=True, config=config
        ) == pytest.approx(100.1)

    def test_long_exit_is_filled_lower(self):
        config = TradingCostConfig(slippage_bps=10.0)

        assert apply_slippage(
            100.0, side=LONG, is_entry=False, config=config
        ) == pytest.approx(99.9)

    def test_enabling_slippage_reduces_net_pnl(self):
        common = dict(entry_price=100.0, exit_price=130.0, quantity=0.1)

        without = compute_trade_costs(
            **common,
            config=TradingCostConfig(
                slippage_bps=0.0, slippage_enabled=False
            ),
        )
        with_slippage = compute_trade_costs(
            **common,
            config=TradingCostConfig(
                slippage_bps=5.0, slippage_enabled=True
            ),
        )

        assert with_slippage["net_pnl"] < without["net_pnl"]
        assert with_slippage["slippage_cost"] > 0
        assert without["slippage_cost"] == 0.0

    def test_disabling_slippage_is_honoured(self):
        config = TradingCostConfig(
            slippage_bps=50.0,
            slippage_enabled=False,
        )

        assert config.slippage_rate == 0.0


class TestConfiguration:

    def test_config_is_read_from_environment(self, monkeypatch):
        monkeypatch.setenv("PAPER_TAKER_FEE_RATE", "0.002")
        monkeypatch.setenv("PAPER_MAKER_FEE_RATE", "0.0005")
        monkeypatch.setenv("PAPER_SLIPPAGE_BPS", "12.5")
        monkeypatch.setenv("PAPER_ENTRY_LIQUIDITY", "maker")

        config = TradingCostConfig.from_env()

        assert config.taker_fee_rate == 0.002
        assert config.maker_fee_rate == 0.0005
        assert config.slippage_bps == 12.5
        assert config.entry_liquidity == MAKER

    @pytest.mark.parametrize(
        "bad_value",
        ["abc", "-1", "nan", "inf"],
    )
    def test_invalid_config_falls_back_to_conservative_default(
        self, monkeypatch, bad_value
    ):
        """
        Некорректное значение не должно молча ОБНУЛИТЬ комиссию —
        это завысило бы результат. Берётся консервативный default.
        """
        monkeypatch.setenv("PAPER_TAKER_FEE_RATE", bad_value)

        config = TradingCostConfig.from_env()

        assert config.taker_fee_rate == (
            TradingCostConfig.DEFAULT_TAKER_FEE_RATE
        )
        assert config.taker_fee_rate > 0

    def test_snapshot_contains_effective_configuration(self):
        snapshot = TradingCostConfig().snapshot()

        for field in (
            "fee_model_version",
            "maker_fee_rate",
            "taker_fee_rate",
            "slippage_bps",
            "slippage_enabled",
            "slippage_rate",
            "entry_liquidity",
            "exit_liquidity",
        ):
            assert field in snapshot

    def test_exchange_rates_are_not_hardcoded_in_the_computation(
        self, monkeypatch
    ):
        """Смена тарифа в окружении обязана менять результат."""
        monkeypatch.setenv("PAPER_TAKER_FEE_RATE", "0.01")

        expensive = compute_trade_costs(
            entry_price=100.0,
            exit_price=130.0,
            quantity=0.1,
            config=TradingCostConfig.from_env(),
        )

        monkeypatch.setenv("PAPER_TAKER_FEE_RATE", "0.0001")

        cheap = compute_trade_costs(
            entry_price=100.0,
            exit_price=130.0,
            quantity=0.1,
            config=TradingCostConfig.from_env(),
        )

        assert expensive["total_fees"] > cheap["total_fees"]
        assert expensive["net_pnl"] < cheap["net_pnl"]


class TestClosedTradeRecord:
    """Закрытая сделка обязана нести полный разбор издержек."""

    REQUIRED_FIELDS = (
        "entry_price_raw",
        "entry_price_effective",
        "exit_price_raw",
        "exit_price_effective",
        "quantity",
        "gross_pnl",
        "entry_fee",
        "exit_fee",
        "total_fees",
        "slippage_cost",
        "net_pnl",
        "fee_model_version",
        "cost_config",
    )

    def close_trade(self, tmp_path: Path, **kwargs):
        manager = PaperPositionManager(
            state_file=tmp_path / "pos.json",
            **kwargs,
        )
        manager.open_position(
            build_paper_order(),
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        return manager.evaluate_position(
            market_price=125.0,
            candle_high=131.0,
            candle_low=100.0,
            observed_at_utc="2026-07-28T12:05:00+00:00",
        )

    def test_all_required_fields_are_recorded(self, tmp_path):
        position = self.close_trade(tmp_path)["position"]

        for field in self.REQUIRED_FIELDS:
            assert field in position, field

    def test_realized_pnl_is_net_and_gross_is_kept(self, tmp_path):
        position = self.close_trade(tmp_path)["position"]

        assert position["gross_pnl"] == pytest.approx(3.0)
        assert position["net_pnl"] < position["gross_pnl"]
        assert position["realized_pnl"] == position["net_pnl"]

    def test_exact_net_matches_independent_calculation(self, tmp_path):
        """
        Ожидание посчитано вручную, а не скопировано из вывода:
          entry_eff = 100 * 1.0005 = 100.05
          exit_eff  = 130 * 0.9995 = 129.935
          slippage  = (0.05 + 0.065) * 0.1        = 0.0115
          fees      = (100.05 + 129.935) * 0.1 * 0.001 = 0.0229985
          net       = 3.0 - 0.0115 - 0.0229985    = 2.9655015
        """
        position = self.close_trade(
            tmp_path,
            cost_config=TradingCostConfig(),
        )["position"]

        assert position["entry_price_effective"] == pytest.approx(100.05)
        assert position["exit_price_effective"] == pytest.approx(129.935)
        assert position["slippage_cost"] == pytest.approx(0.0115)
        assert position["total_fees"] == pytest.approx(0.0229985)
        assert position["net_pnl"] == pytest.approx(2.9655015)

    def test_configuration_snapshot_is_stored_with_the_trade(
        self, tmp_path
    ):
        position = self.close_trade(
            tmp_path,
            cost_config=TradingCostConfig(
                taker_fee_rate=0.003,
                slippage_bps=7.0,
            ),
        )["position"]

        snapshot = position["cost_config"]

        assert snapshot["taker_fee_rate"] == 0.003
        assert snapshot["slippage_bps"] == 7.0
        assert position["fee_model_version"] == (
            TradingCostConfig.VERSION
        )

    def test_zero_cost_config_reproduces_the_old_arithmetic(
        self, tmp_path
    ):
        position = self.close_trade(
            tmp_path,
            cost_config=TradingCostConfig.zero_cost(),
        )["position"]

        assert position["gross_pnl"] == pytest.approx(3.0)
        assert position["net_pnl"] == pytest.approx(3.0)
        assert position["realized_pnl"] == pytest.approx(3.0)


class TestHistoricalRecordsAreNotRecomputed:

    def test_cost_model_never_rewrites_existing_journal(self, tmp_path):
        """
        Прежние INVALID_TEST_DATA записи не пересчитываются: модель
        применяется только в момент закрытия новой сделки и ничего не
        читает из журнала.
        """
        journal = tmp_path / "paper_runs.jsonl"
        original = (
            '{"marker": "INVALID_TEST_DATA_END", "realized_pnl": 3.0}\n'
        )
        journal.write_text(original, encoding="utf-8")

        manager = PaperPositionManager(state_file=tmp_path / "pos.json")
        manager.open_position(
            build_paper_order(),
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )
        manager.evaluate_position(
            market_price=125.0,
            candle_high=131.0,
            candle_low=100.0,
        )

        assert journal.read_text(encoding="utf-8") == original
