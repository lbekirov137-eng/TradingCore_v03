"""
Тесты HTTP-эндпоинтов наблюдения и супервизора.

Проверяется в первую очередь то, что эндпоинты БЕЗОПАСНЫ:
  - ни один не открывает позицию и не переключает стратегию;
  - каждый отвечает при ПУСТОМ журнале (в контейнере data/ эфемерна,
    поэтому пустой журнал — штатное состояние после рестарта);
  - незарегистрированная стратегия отвергается, а не подставляется.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """
    Клиент с журналом в tmp_path.

    PAPER_DATA_DIR переопределяется ДО импорта роутов, потому что путь
    читается на каждый запрос (journal_path), а не кэшируется.
    """
    monkeypatch.setenv("PAPER_DATA_DIR", str(tmp_path))

    from api.server import app

    return TestClient(app)


def write_journal(tmp_path, records):
    journal = tmp_path / "paper_runs.jsonl"

    journal.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    return journal


def closed_trade_record(utc: str, net_pnl: float) -> dict:
    return {
        "recorded_at_utc": utc,
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "real_order_sent": False,
        "pipeline": {
            "decision": {"decision": "TRADE"},
            "paper_order": {"signal": "BUY", "side": "LONG"},
            "unified_market_context": {
                "market_regime": "TREND",
                "atr_percent": 0.8,
                "relative_volume": 1.2,
            },
        },
        "position_event": {
            "event": "POSITION_CLOSED",
            "realized_pnl": net_pnl,
            "net_pnl": net_pnl,
            "gross_pnl": net_pnl + 0.2,
            "total_fees": 0.15,
            "slippage_cost": 0.05,
            "position": {
                "side": "LONG",
                "status": "CLOSED",
                "entry": 100.0,
                "stop": 90.0,
                "quantity": 0.1,
                "risk_amount": 1.0,
                "net_pnl": net_pnl,
                "realized_pnl": net_pnl,
                "total_fees": 0.15,
                "slippage_cost": 0.05,
            },
        },
    }


class TestPerformanceEndpoint:

    def test_empty_journal_is_answered_not_an_error(self, client) -> None:
        response = client.get("/performance")

        assert response.status_code == 200

        body = response.json()

        assert body["cycles"]["total"] == 0
        assert body["sample"]["verdict"] == "INSUFFICIENT_SAMPLE"
        assert body["real_orders_enabled"] is False

    def test_metrics_are_computed_from_the_journal(
        self, client, tmp_path
    ) -> None:
        write_journal(
            tmp_path,
            [
                closed_trade_record("2026-07-01T10:00:00+00:00", 3.0),
                closed_trade_record("2026-07-02T10:00:00+00:00", -1.0),
            ],
        )

        body = client.get("/performance").json()

        assert body["trades"]["closed"] == 2
        assert body["trades"]["wins"] == 1
        assert body["trades"]["net_pnl"] == 2.0
        assert body["trades"]["profit_factor"] == 3.0

    def test_text_format_is_human_readable(self, client, tmp_path) -> None:
        write_journal(
            tmp_path,
            [closed_trade_record("2026-07-01T10:00:00+00:00", 1.0)],
        )

        response = client.get("/performance?format=text")

        assert response.status_code == 200
        assert "PAPER PERFORMANCE REPORT" in response.text
        assert "INSUFFICIENT_SAMPLE" in response.text

    def test_real_order_in_journal_surfaces_stop(
        self, client, tmp_path
    ) -> None:
        breach = closed_trade_record("2026-07-01T10:00:00+00:00", 1.0)
        breach["real_order_sent"] = True

        write_journal(tmp_path, [breach])

        body = client.get("/performance").json()

        assert body["safety_status"] == "STOP"


class TestStrategyEndpoints:

    def test_status_lists_the_registry_and_guarantees(self, client) -> None:
        body = client.get("/strategies/status").json()

        assert body["mode"] == "PAPER"
        assert body["real_orders_enabled"] is False
        assert body["automatic_switching"] == "PAPER_ONLY"
        assert len(body["registry"]) == 5
        assert body["guarantees"]

        # Исторические заметки не потеряны.
        assert "research_only" in body
        assert body["research_only"]["ORB_LEGACY"]["production_approved"] is False

    def test_unregistered_champion_is_refused(self, client) -> None:
        body = client.get(
            "/strategies/status?champion=INVENTED_BY_AI"
        ).json()

        assert body["error"] == "UNREGISTERED_STRATEGY"
        assert "RANGE_NO_TRADE_POLICY" in body["registered"]

    def test_performance_exposes_thresholds_next_to_metrics(
        self, client
    ) -> None:
        body = client.get("/strategies/performance").json()

        assert body["thresholds"]["sample"]["min_closed_trades_pause"] == 50
        assert body["thresholds"]["promote"]["min_profit_factor"] == 1.15
        assert body["insufficient_sample"] == "INSUFFICIENT_SAMPLE"

    def test_change_report_is_produced_without_any_switch(
        self, client
    ) -> None:
        body = client.get("/strategies/change-report").json()

        assert body["schema_version"] == "STRATEGY_CHANGE_REPORT_V1"
        assert body["switch_allowed"] is False
        assert body["mode"] == "PAPER"
        assert body["real_orders_enabled"] is False
        assert body["reason"]

    def test_endpoints_never_report_a_real_order(self, client) -> None:
        for path in (
            "/performance",
            "/strategies/status",
            "/strategies/performance",
            "/strategies/change-report",
        ):
            body = client.get(path).json()

            assert body.get("real_orders_enabled") in (False, None), path
