"""
Регрессия: PaperPositionManager отвергал КАЖДЫЙ валидный длинный ордер.

Корень: контракт paper_order разделил поля —
    signal = BUY/SELL   (торговый сигнал)
    side   = LONG/SHORT (направление позиции)
— а проверка в PaperPositionManager осталась на старом контракте, где
"side" переиспользовалось под значение сигнала и сравнивалось с "BUY".

Следствие: side == "LONG" != "BUY" -> ValueError на каждой свече с
решением TRADE, ни одна paper-позиция не могла быть открыта, а журнал
заполнялся записями FAILED_SAFELY вместо честных решений.

Тесты ниже:
  1) воспроизводят ровно тот ордер, который выдаёт живой пайплайн;
  2) фиксируют обратную совместимость со старым контрактом;
  3) подтверждают, что SELL/SHORT по-прежнему отвергается —
     торговая семантика SPOT_LONG_ONLY не ослаблена.
"""

from pathlib import Path

import pytest

from api.core.bootstrap import Bootstrap
from api.paper_trading.position_manager import (
    PaperPositionManager,
)
from dry_run import build_context


def build_manager(tmp_path: Path) -> PaperPositionManager:
    return PaperPositionManager(
        state_file=tmp_path / "paper_position.json"
    )


def build_current_contract_order() -> dict:
    """Новый контракт: signal и side разделены."""
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


class TestCurrentContractIsAccepted:

    def test_long_order_from_current_contract_opens_position(
        self, tmp_path: Path
    ) -> None:
        """
        Это и есть воспроизведение бага: до исправления здесь падало
        ValueError("Only BUY paper orders are supported").
        """
        manager = build_manager(tmp_path)

        result = manager.open_position(
            build_current_contract_order(),
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        assert result["event"] == "POSITION_OPENED"
        assert result["real_order_sent"] is False
        assert result["position"]["status"] == "OPEN"
        assert result["position"]["side"] == "LONG"

    def test_real_pipeline_order_is_accepted_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """
        Самый ценный тест: ордер берётся не из фикстуры, а из реального
        прогона пайплайна. Именно это расхождение и осталось незамеченным —
        фикстуры в тестах использовали старый контракт, а живой пайплайн
        уже выдавал новый.
        """
        result = Bootstrap.build().execute(build_context())

        assert result.decision["decision"] == "TRADE"

        paper_order = result.execution["paper_order"]

        # Фиксируем фактическую форму контракта, чтобы будущий дрейф
        # ломал тест здесь, а не молча в рантайме.
        assert paper_order["signal"] == "BUY"
        assert paper_order["side"] == "LONG"

        manager = build_manager(tmp_path)

        opened = manager.open_position(
            paper_order,
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        assert opened["event"] == "POSITION_OPENED"
        assert opened["real_order_sent"] is False


class TestLegacyContractStillWorks:

    def test_legacy_side_buy_is_still_accepted(
        self, tmp_path: Path
    ) -> None:
        """Правка обязана быть совместимой, а не подменять контракт."""
        legacy_order = build_current_contract_order()
        legacy_order.pop("signal")
        legacy_order["side"] = "BUY"

        manager = build_manager(tmp_path)

        result = manager.open_position(
            legacy_order,
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        assert result["event"] == "POSITION_OPENED"
        assert result["position"]["side"] == "BUY"


class TestShortIsStillRefused:
    """
    Ключевая проверка безопасности: исправление НЕ должно открыть путь
    для SELL/SHORT. Режим остаётся SPOT_LONG_ONLY.
    """

    def test_short_side_is_refused(self, tmp_path: Path) -> None:
        order = build_current_contract_order()
        order["signal"] = "SELL"
        order["side"] = "SHORT"

        with pytest.raises(ValueError, match="Only BUY paper orders"):
            build_manager(tmp_path).open_position(order)

    def test_legacy_sell_side_is_refused(self, tmp_path: Path) -> None:
        order = build_current_contract_order()
        order.pop("signal")
        order["side"] = "SELL"

        with pytest.raises(ValueError, match="Only BUY paper orders"):
            build_manager(tmp_path).open_position(order)

    def test_sell_signal_wins_over_long_side(self, tmp_path: Path) -> None:
        """
        Противоречивый ордер (signal=SELL, side=LONG) не должен
        проскочить: приоритет у signal, и он отвергается.
        """
        order = build_current_contract_order()
        order["signal"] = "SELL"
        order["side"] = "LONG"

        with pytest.raises(ValueError, match="Only BUY paper orders"):
            build_manager(tmp_path).open_position(order)

    def test_missing_direction_is_refused(self, tmp_path: Path) -> None:
        order = build_current_contract_order()
        order.pop("signal")
        order.pop("side")

        with pytest.raises(ValueError, match="Only BUY paper orders"):
            build_manager(tmp_path).open_position(order)

    def test_none_direction_is_refused(self, tmp_path: Path) -> None:
        order = build_current_contract_order()
        order["signal"] = None
        order["side"] = None

        with pytest.raises(ValueError, match="Only BUY paper orders"):
            build_manager(tmp_path).open_position(order)


class TestSignalOnlyOrderIsFullySupported:
    """
    Вторая половина той же миграции контракта.

    Валидация признаёт ордер валидным по одному лишь `signal` — "side"
    объявлен НЕОБЯЗАТЕЛЬНЫМ fallback. Но сборка позиции читала
    paper_order["side"] напрямую, поэтому ордер, только что прошедший
    валидацию, падал с KeyError('side').

    Это хуже обычного отказа: KeyError — не тот контролируемый ValueError,
    который цикл ожидает и записывает как честное решение, а
    необработанное исключение. Тесты фиксируют, что форма, объявленная
    допустимой, действительно работает от валидации до записи состояния.
    """

    def test_signal_only_order_opens_position(
        self, tmp_path: Path
    ) -> None:
        """До исправления здесь падало KeyError('side')."""
        order = build_current_contract_order()
        order.pop("side")

        result = build_manager(tmp_path).open_position(
            order,
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        assert result["event"] == "POSITION_OPENED"
        assert result["real_order_sent"] is False
        # Направление выводится из signal=BUY, а не выдумывается.
        assert result["position"]["side"] == "LONG"

    def test_signal_only_order_survives_a_full_round_trip(
        self, tmp_path: Path
    ) -> None:
        """
        Записанная позиция обязана быть пригодной для закрытия: если бы
        side уехал в None, normalise_side в cost_model всё равно вернул
        бы LONG, но знак результата стоит проверить явно.
        """
        order = build_current_contract_order()
        order.pop("side")

        manager = build_manager(tmp_path)
        manager.open_position(
            order,
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        closed = manager.evaluate_position(
            market_price=130.0,
            candle_high=130.0,
            candle_low=125.0,
            observed_at_utc="2026-07-28T12:05:00+00:00",
        )

        assert closed["event"] == "POSITION_CLOSED"
        assert closed["exit_reason"] == "TAKE_PROFIT_2"
        # Лонг, закрытый выше входа, обязан дать положительный gross.
        assert closed["gross_pnl"] > 0
        assert closed["position"]["side"] == "LONG"

    def test_explicit_side_is_never_rewritten(
        self, tmp_path: Path
    ) -> None:
        """
        Правка только читает side, а не выводит его заново: устаревшее
        "BUY" сохраняется как есть, чтобы не переписывать задним числом
        уже существующие позиции и записи журнала.
        """
        legacy_order = build_current_contract_order()
        legacy_order.pop("signal")
        legacy_order["side"] = "BUY"

        result = build_manager(tmp_path).open_position(
            legacy_order,
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        assert result["position"]["side"] == "BUY"

    def test_blank_side_falls_back_instead_of_storing_junk(
        self, tmp_path: Path
    ) -> None:
        """
        signal задаёт направление, а side пустой/None. Валидация такой
        ордер пропускает (приоритет у signal), поэтому в состояние обязано
        попасть осмысленное направление, а не None или пустая строка.
        """
        # Отдельный каталог на случай, чтобы позиции не мешали друг другу.
        # Имя — индекс, а не само значение: пустая строка и пробелы не
        # являются корректными именами файлов в Windows.
        for index, blank in enumerate((None, "", "   ")):
            order = build_current_contract_order()
            order["side"] = blank

            manager = build_manager(tmp_path / f"case_{index}")

            result = manager.open_position(
                order,
                opened_at_utc="2026-07-28T12:00:00+00:00",
            )

            assert result["position"]["side"] == "LONG", (
                f"side={blank!r} должен был дать LONG"
            )

    def test_signal_only_short_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        """
        Умолчание "LONG" не должно превратить SELL-ордер без side в лонг:
        он обязан быть отвергнут ВАЛИДАЦИЕЙ, до записи направления.
        """
        order = build_current_contract_order()
        order.pop("side")
        order["signal"] = "SELL"

        with pytest.raises(ValueError, match="Only BUY paper orders"):
            build_manager(tmp_path).open_position(order)


class TestRealOrdersRemainImpossible:

    def test_order_claiming_a_real_order_is_refused(
        self, tmp_path: Path
    ) -> None:
        order = build_current_contract_order()
        order["real_order_sent"] = True

        with pytest.raises(ValueError, match="must not send"):
            build_manager(tmp_path).open_position(order)

    def test_non_paper_mode_is_refused(self, tmp_path: Path) -> None:
        order = build_current_contract_order()
        order["mode"] = "LIVE"

        with pytest.raises(ValueError, match="mode must be PAPER"):
            build_manager(tmp_path).open_position(order)

    def test_opened_position_never_marks_a_real_order(
        self, tmp_path: Path
    ) -> None:
        result = build_manager(tmp_path).open_position(
            build_current_contract_order(),
            opened_at_utc="2026-07-28T12:00:00+00:00",
        )

        assert result["real_order_sent"] is False
        assert result["position"]["real_order_sent"] is False
        assert result["position"]["mode"] == "PAPER"
        assert (
            result["position"]["execution_mode"]
            == "SPOT_LONG_ONLY"
        )
