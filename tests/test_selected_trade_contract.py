"""
Контракт selected_trade.

Раньше каждый шаг читал сырой словарь и сам выводил направление из
сигнала. Формула `side` была скопирована в четыре места, и одна из копий
отстала: PaperPositionManager сравнивал side с "BUY", тогда как конвейер
уже клал "LONG", из-за чего отвергался КАЖДЫЙ валидный лонг.

Каноническая модель убирает саму возможность такого расхождения: side не
хранится, а вычисляется в одном месте. Тесты ниже фиксируют строгость
разбора — молча принятый мусор превращается в сделку, поэтому
permissive-поведение здесь опаснее падения.
"""

import pytest

from api.contracts.selected_trade import (
    BUY,
    LONG,
    NO_TRADE,
    NONE,
    SELL,
    SHORT,
    SelectedTrade,
    SelectedTradeError,
    normalise_legacy_strategy,
    side_for_signal,
)


def canonical(**overrides) -> dict:
    payload = {
        "strategy": "EMA_AND_VLAD_ORB",
        "signal": BUY,
        "entry": 101.1,
        "stop": 100.2,
        "take_profit_1": 102.9,
        "take_profit_2": 103.8,
        "risk_reward": "1:2 / 1:3",
        "reason": "test",
    }
    payload.update(overrides)
    return payload


class TestSideNormalisation:
    """Единая семантика направления — источник исходного дефекта."""

    @pytest.mark.parametrize(
        "signal,expected",
        [(BUY, LONG), (SELL, SHORT), (NO_TRADE, NONE)],
    )
    def test_side_is_derived_from_signal(self, signal, expected):
        assert side_for_signal(signal) == expected

    @pytest.mark.parametrize("signal", [None, "", "buy", "UNKNOWN", 0])
    def test_unknown_signal_is_never_tradable_direction(self, signal):
        assert side_for_signal(signal) == NONE

    def test_side_is_computed_not_stored(self):
        """
        Даже если во входном словаре лежит ПРОТИВОРЕЧИВЫЙ side, модель
        берёт направление из сигнала. Хранимое поле не может разойтись
        с сигналом, потому что оно не хранится.
        """
        trade = SelectedTrade.from_mapping(
            canonical(signal=BUY, side="SHORT")
        )

        assert trade.side == LONG
        assert trade.to_dict()["side"] == LONG


class TestValidPayloads:

    def test_full_canonical_payload(self):
        trade = SelectedTrade.from_mapping(canonical())

        assert trade.strategy == "EMA_AND_VLAD_ORB"
        assert trade.signal == BUY
        assert trade.side == LONG
        assert trade.entry == 101.1
        assert trade.is_tradable is True
        assert trade.has_levels is True

    def test_levels_may_be_absent_for_ema_branch(self):
        """
        Координатор штатно возвращает сделку без уровней для EMA — их
        считает TradePlanStep из цены и ATR. Отсутствие уровней и НУЛЕВЫЕ
        уровни это разные вещи, поэтому None допустим.
        """
        trade = SelectedTrade.from_mapping(
            canonical(
                strategy="EMA",
                entry=None,
                stop=None,
                take_profit_1=None,
                take_profit_2=None,
            )
        )

        assert trade.has_levels is False
        assert trade.is_tradable is True

    def test_round_trip_through_dict(self):
        trade = SelectedTrade.from_mapping(canonical())
        again = SelectedTrade.from_mapping(trade.to_dict())

        assert again == trade

    def test_real_orders_are_always_false(self):
        trade = SelectedTrade.from_mapping(
            canonical(real_order_sent=True)
        )

        assert trade.real_order_sent is False
        assert trade.to_dict()["real_order_sent"] is False


class TestNoTrade:

    def test_no_trade_is_not_tradable(self):
        trade = SelectedTrade.from_mapping(
            canonical(
                signal=NO_TRADE,
                entry=None,
                stop=None,
                take_profit_1=None,
                take_profit_2=None,
                risk_reward=NO_TRADE,
            )
        )

        assert trade.is_tradable is False
        assert trade.side == NONE

    def test_no_trade_is_a_valid_payload(self):
        """NO TRADE — штатный исход, а не ошибка разбора."""
        SelectedTrade.from_mapping(canonical(signal=NO_TRADE))


class TestInvalidPayloadsAreRejected:
    """Строгость намеренная: подставлять умолчания здесь опасно."""

    @pytest.mark.parametrize(
        "raw",
        [None, [], "BUY", 42, object()],
    )
    def test_non_mapping_is_rejected(self, raw):
        with pytest.raises(SelectedTradeError, match="must be a mapping"):
            SelectedTrade.from_mapping(raw)

    @pytest.mark.parametrize(
        "signal",
        [None, "", "buy", "UNKNOWN", 0, True],
    )
    def test_invalid_signal_is_rejected(self, signal):
        with pytest.raises(SelectedTradeError, match="signal must be one of"):
            SelectedTrade.from_mapping(canonical(signal=signal))

    def test_missing_signal_is_rejected(self):
        payload = canonical()
        del payload["signal"]

        with pytest.raises(SelectedTradeError, match="signal must be one of"):
            SelectedTrade.from_mapping(payload)

    @pytest.mark.parametrize("strategy", [None, "", 42, []])
    def test_invalid_strategy_is_rejected(self, strategy):
        with pytest.raises(
            SelectedTradeError, match="strategy must be a non-empty string"
        ):
            SelectedTrade.from_mapping(canonical(strategy=strategy))

    @pytest.mark.parametrize(
        "value",
        ["101.1", True, [], float("nan"), float("inf")],
    )
    def test_non_numeric_or_non_finite_level_is_rejected(self, value):
        with pytest.raises(SelectedTradeError, match="entry"):
            SelectedTrade.from_mapping(canonical(entry=value))

    def test_invalid_risk_reward_is_rejected(self):
        with pytest.raises(
            SelectedTradeError, match="risk_reward must be a string"
        ):
            SelectedTrade.from_mapping(canonical(risk_reward=13))

    def test_invalid_reason_is_rejected(self):
        with pytest.raises(SelectedTradeError, match="reason must be"):
            SelectedTrade.from_mapping(canonical(reason=13))


class TestReadingFromContextStrategy:

    def test_reads_canonical_selected_trade(self):
        trade = SelectedTrade.from_context_strategy(
            {"signal": BUY, "selected_trade": canonical()}
        )

        assert trade.signal == BUY

    def test_missing_selected_trade_is_an_error_not_a_default(self):
        """
        Отсутствие selected_trade означает, что StrategyCoordinatorStep
        не отработал, то есть конвейер сломан. Собирать сделку из чего
        попало в этой ситуации опаснее, чем упасть.
        """
        with pytest.raises(
            SelectedTradeError, match="missing selected_trade"
        ):
            SelectedTrade.from_context_strategy({"signal": BUY})

    def test_non_mapping_strategy_is_rejected(self):
        with pytest.raises(
            SelectedTradeError, match="context.strategy must be a mapping"
        ):
            SelectedTrade.from_context_strategy(None)


class TestLegacyMigration:

    def test_legacy_signal_only_maps_to_ema_without_levels(self):
        """
        Перевод однозначен и не требует выдумывать значения: ровно так
        координатор описывает ветку EMA — сигнал есть, уровней нет.
        """
        trade = normalise_legacy_strategy({"signal": BUY})

        assert trade.strategy == "EMA"
        assert trade.signal == BUY
        assert trade.side == LONG
        assert trade.has_levels is False
        assert trade.entry is None

    def test_legacy_no_trade_is_preserved(self):
        trade = normalise_legacy_strategy({"signal": NO_TRADE})

        assert trade.is_tradable is False

    def test_canonical_payload_passes_through_untouched(self):
        strategy = {"signal": BUY, "selected_trade": canonical()}

        assert normalise_legacy_strategy(strategy) == (
            SelectedTrade.from_mapping(canonical())
        )

    def test_legacy_invalid_signal_still_rejected(self):
        """Адаптер не смягчает валидацию."""
        with pytest.raises(SelectedTradeError):
            normalise_legacy_strategy({"signal": "UNKNOWN"})

    def test_legacy_adapter_never_fabricates_levels(self):
        trade = normalise_legacy_strategy({"signal": BUY})

        for field in ("entry", "stop", "take_profit_1", "take_profit_2"):
            assert getattr(trade, field) is None, field


class TestImmutability:

    def test_model_is_frozen(self):
        trade = SelectedTrade.from_mapping(canonical())

        with pytest.raises(Exception):
            trade.signal = SELL
