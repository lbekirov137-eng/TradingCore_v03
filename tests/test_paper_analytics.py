"""
Тесты модуля наблюдения и оценки paper-результатов.

Проверяются три класса свойств:

  1. Извлечение. Наблюдение обязано доставать поля из ВЛОЖЕННОЙ записи и
     не падать на записи об ошибке, где pipeline отсутствует вовсе.

  2. Честность метрик. Отсутствие данных — это None, а не 0.0. Ноль
     выглядит как измеренный результат и портит любые выводы.

  3. Защитные статусы. STOP обязан срабатывать на реальном ордере, шорте,
     повторяющемся FAILED_SAFELY и противоречии signal/decision — и НЕ
     обязан срабатывать на здоровом журнале.
"""

from pathlib import Path

from api.paper_analytics import (
    INSUFFICIENT_SAMPLE,
    SAFE,
    STOP,
    WARNING,
    build_observation,
    build_report,
    load_records,
    render_report_text,
)


def cycle_record(
    *,
    decision: str = "TRADE",
    signal: str = "BUY",
    side: str = "LONG",
    event: str = "POSITION_REMAINS_OPEN",
    utc: str = "2026-07-29T10:00:00+00:00",
    net_pnl: float | None = None,
    real_order_sent: bool = False,
    reason: str | None = None,
) -> dict:
    """Полная запись цикла в той же форме, что пишет paper_live_loop."""
    position = {
        "status": "CLOSED" if event == "POSITION_CLOSED" else "OPEN",
        "side": side,
        "entry": 100.0,
        "stop": 90.0,
        "take_profit_1": 120.0,
        "take_profit_2": 130.0,
        "quantity": 0.1,
        "risk_amount": 1.0,
        "unrealized_pnl": 0.5,
        "real_order_sent": real_order_sent,
    }

    position_event: dict = {
        "event": event,
        "position": position,
        "real_order_sent": real_order_sent,
    }

    if reason is not None:
        position_event["reason"] = reason

    if net_pnl is not None:
        position_event.update(
            {
                "realized_pnl": net_pnl,
                "net_pnl": net_pnl,
                "gross_pnl": net_pnl + 0.2,
                "total_fees": 0.15,
                "slippage_cost": 0.05,
                "exit_price": 130.0,
                "exit_reason": "TAKE_PROFIT_2",
            }
        )
        position.update(
            {
                "realized_pnl": net_pnl,
                "net_pnl": net_pnl,
                "gross_pnl": net_pnl + 0.2,
                "total_fees": 0.15,
                "slippage_cost": 0.05,
            }
        )

    return {
        "recorded_at_utc": utc,
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "market_price": 101.0,
        "real_order_sent": real_order_sent,
        "pipeline": {
            "decision": {"decision": decision, "reason": reason},
            "paper_order": {"signal": signal, "side": side},
            "strategy": {"selected_trade": {"signal": signal, "side": side}},
            "unified_market_context": {
                "market_regime": "TREND",
                "atr_percent": 0.85,
                "relative_volume": 1.4,
            },
            "ai_opportunity_review": {"score": 72.5},
        },
        "position_event": position_event,
    }


def failure_record(utc: str = "2026-07-29T10:05:00+00:00") -> dict:
    """Запись об ошибке — без pipeline, как её пишет цикл."""
    return {
        "recorded_at_utc": utc,
        "status": "FAILED_SAFELY",
        "error_type": "ConnectionError",
        "error": "temporary network failure",
        "trade_created": False,
        "real_order_sent": False,
    }


class TestObservationExtraction:

    def test_all_required_fields_are_extracted(self) -> None:
        observation = build_observation(cycle_record())

        assert observation["recorded_at_utc"] == "2026-07-29T10:00:00+00:00"
        assert observation["symbol"] == "BTCUSDT"
        assert observation["timeframe"] == "5m"
        assert observation["market_regime"] == "TREND"
        assert observation["signal"] == "BUY"
        assert observation["decision"] == "TRADE"
        assert observation["side"] == "LONG"
        assert observation["opportunity_score"] == 72.5
        assert observation["atr_percent"] == 0.85
        assert observation["relative_volume"] == 1.4
        assert observation["position_event"] == "POSITION_REMAINS_OPEN"
        assert observation["entry"] == 100.0
        assert observation["stop"] == 90.0
        assert observation["take_profit_1"] == 120.0
        assert observation["take_profit_2"] == 130.0
        assert observation["unrealized_pnl"] == 0.5
        assert observation["real_order_sent"] is False

    def test_costs_are_extracted_from_a_closed_trade(self) -> None:
        observation = build_observation(
            cycle_record(event="POSITION_CLOSED", net_pnl=2.5)
        )

        assert observation["realized_pnl"] == 2.5
        assert observation["net_pnl"] == 2.5
        assert observation["total_fees"] == 0.15
        assert observation["slippage_cost"] == 0.05
        assert observation["exit_reason"] == "TAKE_PROFIT_2"

    def test_failure_record_does_not_crash_and_is_flagged(self) -> None:
        observation = build_observation(failure_record())

        assert observation["failed_safely"] is True
        assert "ConnectionError" in observation["failure_reason"]
        # У записи об ошибке нет ни сигнала, ни решения — и это не ноль.
        assert observation["signal"] is None
        assert observation["decision"] is None
        assert observation["net_pnl"] is None

    def test_missing_values_are_none_not_zero(self) -> None:
        """Ключевое свойство: ноль означал бы «измерено и равно нулю»."""
        observation = build_observation({})

        for field in (
            "atr_percent",
            "relative_volume",
            "opportunity_score",
            "entry",
            "net_pnl",
            "total_fees",
        ):
            assert observation[field] is None, field

    def test_garbage_input_is_tolerated(self) -> None:
        for garbage in (None, [], "text", 42):
            observation = build_observation(garbage)

            assert observation["signal"] is None
            assert observation["real_order_sent"] is False

    def test_no_trade_reason_is_captured(self) -> None:
        observation = build_observation(
            cycle_record(
                decision="NO_TRADE",
                event="NO_POSITION_OPENED",
                reason="NO_TRADE_CANDIDATE",
            )
        )

        assert observation["no_trade_reason"] == "NO_TRADE_CANDIDATE"
        # Отказ по сетапу — это НЕ сбой.
        assert observation["failed_safely"] is False


class TestAggregation:

    def test_counts_cycles_and_decisions(self) -> None:
        report = build_report(
            [
                cycle_record(decision="TRADE"),
                cycle_record(decision="NO_TRADE"),
                cycle_record(decision="NO_TRADE"),
            ]
        )

        assert report["cycles"]["total"] == 3
        assert report["cycles"]["trade_decisions"] == 1
        assert report["cycles"]["no_trade_decisions"] == 2

    def test_win_rate_and_net_pnl_use_closed_trades_only(self) -> None:
        report = build_report(
            [
                cycle_record(event="POSITION_OPENED"),
                cycle_record(event="POSITION_CLOSED", net_pnl=3.0),
                cycle_record(event="POSITION_CLOSED", net_pnl=-1.0),
                cycle_record(event="POSITION_CLOSED", net_pnl=2.0),
            ]
        )

        trades = report["trades"]

        assert trades["opened"] == 1
        assert trades["closed"] == 3
        assert trades["wins"] == 2
        assert trades["losses"] == 1
        assert trades["win_rate_percent"] == round(2 / 3 * 100, 2)
        assert trades["net_pnl"] == 4.0

    def test_profit_factor_and_average_r(self) -> None:
        report = build_report(
            [
                cycle_record(event="POSITION_CLOSED", net_pnl=3.0),
                cycle_record(event="POSITION_CLOSED", net_pnl=-1.5),
            ]
        )

        # 3.0 / 1.5 == 2.0
        assert report["trades"]["profit_factor"] == 2.0
        # risk_amount == 1.0, поэтому R == net: (3.0 + -1.5) / 2
        assert report["trades"]["average_r"] == 0.75

    def test_max_drawdown_and_loss_streak(self) -> None:
        report = build_report(
            [
                cycle_record(event="POSITION_CLOSED", net_pnl=5.0),
                cycle_record(event="POSITION_CLOSED", net_pnl=-2.0),
                cycle_record(event="POSITION_CLOSED", net_pnl=-3.0),
                cycle_record(event="POSITION_CLOSED", net_pnl=1.0),
            ]
        )

        # Пик 5.0, дно 0.0 -> просадка 5.0; подряд два убытка.
        assert report["trades"]["max_drawdown"] == 5.0
        assert report["trades"]["max_loss_streak"] == 2

    def test_metrics_are_none_without_trades(self) -> None:
        report = build_report([cycle_record(decision="NO_TRADE")])

        trades = report["trades"]

        assert trades["closed"] == 0
        assert trades["win_rate_percent"] is None
        assert trades["profit_factor"] is None
        assert trades["average_r"] is None
        assert trades["max_drawdown"] is None

    def test_empty_journal_is_handled(self) -> None:
        report = build_report([])

        assert report["cycles"]["total"] == 0
        assert report["safety_status"] == SAFE
        assert report["sample"]["verdict"] == INSUFFICIENT_SAMPLE

    def test_no_trade_reasons_are_counted(self) -> None:
        report = build_report(
            [
                cycle_record(decision="NO_TRADE", reason="NO_TRADE_CANDIDATE"),
                cycle_record(decision="NO_TRADE", reason="NO_TRADE_CANDIDATE"),
                cycle_record(decision="NO_TRADE", reason="REGIME_BLOCKED"),
            ]
        )

        assert report["reasons"]["no_trade"]["NO_TRADE_CANDIDATE"] == 2
        assert report["reasons"]["no_trade"]["REGIME_BLOCKED"] == 1


class TestInsufficientSample:

    def test_below_threshold_is_reported_with_exact_count(self) -> None:
        report = build_report(
            [cycle_record(event="POSITION_CLOSED", net_pnl=1.0)] * 4
        )

        sample = report["sample"]

        assert sample["sufficient"] is False
        assert sample["verdict"] == INSUFFICIENT_SAMPLE
        assert sample["closed_trades"] == 4
        assert sample["required_closed_trades"] == 30
        assert sample["missing_closed_trades"] == 26

    def test_at_threshold_sample_is_sufficient(self) -> None:
        report = build_report(
            [cycle_record(event="POSITION_CLOSED", net_pnl=1.0)] * 30
        )

        assert report["sample"]["sufficient"] is True
        assert report["sample"]["verdict"] is None

    def test_insufficient_sample_is_visible_in_text(self) -> None:
        text = render_report_text(
            build_report([cycle_record(event="POSITION_CLOSED", net_pnl=1.0)])
        )

        assert INSUFFICIENT_SAMPLE in text
        assert "closed_trades=1" in text


class TestSafetyStatus:

    def test_healthy_journal_is_safe(self) -> None:
        report = build_report(
            [
                cycle_record(decision="NO_TRADE"),
                cycle_record(event="POSITION_CLOSED", net_pnl=1.0),
            ]
        )

        assert report["safety_status"] == SAFE
        assert report["stop_reasons"] == []

    def test_real_order_sent_forces_stop(self) -> None:
        report = build_report([cycle_record(real_order_sent=True)])

        assert report["safety_status"] == STOP
        assert any("real_order_sent" in r for r in report["stop_reasons"])

    def test_short_direction_forces_stop(self) -> None:
        report = build_report(
            [cycle_record(signal="SELL", side="SHORT")]
        )

        assert report["safety_status"] == STOP
        assert any("SHORT" in r for r in report["stop_reasons"])

    def test_repeated_failed_safely_forces_stop(self) -> None:
        report = build_report(
            [
                failure_record("2026-07-29T10:00:00+00:00"),
                failure_record("2026-07-29T10:05:00+00:00"),
            ]
        )

        assert report["safety_status"] == STOP
        assert any("FAILED_SAFELY" in r for r in report["stop_reasons"])

    def test_isolated_failed_safely_is_only_a_warning(self) -> None:
        """
        Одиночный сетевой сбой — ожидаемое событие, а не поломка. Если бы
        он давал STOP, статус был бы бесполезен: он стоял бы всегда.
        """
        report = build_report(
            [
                failure_record("2026-07-29T10:00:00+00:00"),
                cycle_record(decision="NO_TRADE"),
                failure_record("2026-07-29T10:10:00+00:00"),
            ]
        )

        assert report["safety_status"] == WARNING
        assert report["stop_reasons"] == []

    def test_trade_without_signal_is_a_contradiction(self) -> None:
        record = cycle_record(decision="TRADE")
        record["pipeline"]["paper_order"]["signal"] = "NO TRADE"
        record["pipeline"]["strategy"]["selected_trade"]["signal"] = "NO TRADE"

        report = build_report([record])

        assert report["safety_status"] == STOP
        assert any("contradiction" in r for r in report["stop_reasons"])

    def test_no_trade_decision_with_opened_position_is_a_contradiction(
        self,
    ) -> None:
        report = build_report(
            [cycle_record(decision="NO_TRADE", event="POSITION_OPENED")]
        )

        assert report["safety_status"] == STOP
        assert report["safety"]["contradiction_count"] == 1

    def test_loss_streak_raises_warning_not_stop(self) -> None:
        report = build_report(
            [cycle_record(event="POSITION_CLOSED", net_pnl=-1.0)] * 5
        )

        assert report["safety_status"] == WARNING
        assert any("losing streak" in r for r in report["warning_reasons"])

    def test_stop_wins_over_warning(self) -> None:
        """Статус fail-closed: WARNING не может «перебить» STOP."""
        report = build_report(
            [
                cycle_record(event="POSITION_CLOSED", net_pnl=-1.0),
                cycle_record(real_order_sent=True),
            ]
        )

        assert report["safety_status"] == STOP


class TestJournalLoading:

    def test_reads_jsonl_and_skips_broken_lines(self, tmp_path: Path) -> None:
        import json

        journal = tmp_path / "paper_runs.jsonl"

        journal.write_text(
            "\n".join(
                [
                    json.dumps(cycle_record(decision="NO_TRADE")),
                    "{not valid json",
                    json.dumps(
                        cycle_record(event="POSITION_CLOSED", net_pnl=1.0)
                    ),
                ]
            ),
            encoding="utf-8",
        )

        records = load_records(journal)

        assert len(records) == 3

        report = build_report(records)

        # Битая строка не роняет отчёт, но и не теряется молча.
        assert report["cycles"]["unreadable_journal_lines"] == 1
        assert report["safety_status"] == WARNING
        assert report["trades"]["closed"] == 1

    def test_missing_journal_returns_empty(self, tmp_path: Path) -> None:
        assert load_records(tmp_path / "absent.jsonl") == []

    def test_limit_keeps_the_most_recent_records(self, tmp_path: Path) -> None:
        import json

        journal = tmp_path / "paper_runs.jsonl"

        journal.write_text(
            "\n".join(
                json.dumps(
                    cycle_record(utc=f"2026-07-29T10:0{index}:00+00:00")
                )
                for index in range(5)
            ),
            encoding="utf-8",
        )

        records = load_records(journal, limit=2)

        assert len(records) == 2
        assert records[-1]["recorded_at_utc"] == "2026-07-29T10:04:00+00:00"


class TestTextReport:

    def test_report_contains_required_sections(self) -> None:
        text = render_report_text(
            build_report(
                [
                    cycle_record(decision="NO_TRADE"),
                    cycle_record(event="POSITION_CLOSED", net_pnl=2.0),
                    cycle_record(event="POSITION_CLOSED", net_pnl=-1.0),
                ]
            )
        )

        for fragment in (
            "DAILY PAPER REPORT",
            "STATUS:",
            "CYCLES:",
            "TRADES:",
            "NET PnL (after costs)",
            "WIN RATE",
            "PROFIT FACTOR",
            "AVERAGE R",
            "MAX DRAWDOWN",
            "LOSS STREAK",
            "GUARDS:",
            "real_orders=False",
        ):
            assert fragment in text, fragment

    def test_stop_reasons_are_printed(self) -> None:
        text = render_report_text(
            build_report([cycle_record(real_order_sent=True)])
        )

        assert "STATUS: STOP" in text
        assert "STOP:" in text

    def test_unmeasured_metrics_print_as_na_not_zero(self) -> None:
        text = render_report_text(build_report([]))

        assert "WIN RATE:      n/a" in text
        assert "0.0%" not in text
