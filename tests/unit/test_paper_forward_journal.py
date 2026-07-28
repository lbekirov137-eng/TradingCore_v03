import json

import pytest

from api.observability.paper_forward_journal import (
    PaperForwardJournal,
    resolve_code_commit_hash,
    compute_signal_id,
)
from api.contracts.context import LiveContext


REQUIRED_FIELDS = (
    "timestamp", "exchange", "symbol", "timeframe", "strategy",
    "market_regime", "signal", "decision", "reason",
    "virtual_entry", "virtual_stop", "virtual_take_profit",
    "position_size", "assumed_fees", "assumed_slippage",
    "virtual_net_pnl", "drawdown_percent", "strategy_version",
    "code_commit_hash", "signal_id", "trade_id",
)


def _no_trade_decision():
    return {
        "decision": "NO_TRADE",
        "reason": "Нет сигналов стратегии.",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "strategy": "ORB",
    }


def _trade_decision():
    return {
        "decision": "TRADE",
        "reason": "Сигнал подтверждён.",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "strategy": "ORB",
        "direction": "LONG",
        "trade_plan": {
            "entry": 100.0, "stop_loss": 98.0,
            "take_profit": {"tp1": 104.0, "tp2": 106.0},
        },
        "risk": {"position_size": 1.0, "fee_amount": 0.1, "slippage_amount": 0.05},
        "signature": ("binance", "BTCUSDT", "LONG", 100.0, 98.0),
    }


def _context():
    ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
    ctx.strategy_signals = [{
        "approved": True, "strategy": "ORB", "direction": "LONG",
        "confidence": 0.5, "metadata": {"regime": "TREND_UP"},
    }]
    return ctx


class TestCommitHashResolution:

    def test_resolves_a_non_empty_string(self):
        commit_hash = resolve_code_commit_hash()
        assert isinstance(commit_hash, str)
        assert len(commit_hash) > 0

    def test_env_var_takes_priority(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "deadbeef1234")

        # Bypass the module-level cache to test resolution fresh.
        import api.observability.paper_forward_journal as pfj
        pfj._COMMIT_HASH_CACHE = None

        assert pfj.resolve_code_commit_hash() == "deadbeef1234"

        pfj._COMMIT_HASH_CACHE = None  # reset cache so other tests aren't affected


class TestSignalId:

    def test_deterministic_for_same_inputs(self):
        id1 = compute_signal_id("binance", "BTCUSDT", "ORB", 123456)
        id2 = compute_signal_id("binance", "BTCUSDT", "ORB", 123456)
        assert id1 == id2

    def test_different_for_different_timestamps(self):
        id1 = compute_signal_id("binance", "BTCUSDT", "ORB", 123456)
        id2 = compute_signal_id("binance", "BTCUSDT", "ORB", 999999)
        assert id1 != id2


class TestJournalRecord:

    def test_no_trade_entry_has_all_required_fields(self, tmp_path):
        journal = PaperForwardJournal(path=str(tmp_path / "journal.jsonl"))
        ctx = _context()

        entry = journal.record(ctx, _no_trade_decision(), {"status": "NO_TRADE", "reason": "x"})

        for field in REQUIRED_FIELDS:
            assert field in entry, f"missing field: {field}"

        assert entry["decision"] == "NO_TRADE"
        assert entry["exchange"] == "binance"
        assert entry["symbol"] == "BTCUSDT"

    def test_trade_entry_captures_virtual_plan(self, tmp_path):
        journal = PaperForwardJournal(path=str(tmp_path / "journal.jsonl"))
        ctx = _context()

        entry = journal.record(ctx, _trade_decision(), {"status": "OPENED", "entry_client_order_id": "cid-1"})

        assert entry["decision"] == "TRADE"
        assert entry["virtual_entry"] == 100.0
        assert entry["virtual_stop"] == 98.0
        assert entry["virtual_take_profit"] == 104.0
        assert entry["position_size"] == 1.0
        assert entry["assumed_fees"] == 0.1
        assert entry["assumed_slippage"] == 0.05
        assert entry["trade_id"] == "cid-1"

    def test_strategy_version_and_status_are_populated(self, tmp_path):
        journal = PaperForwardJournal(path=str(tmp_path / "journal.jsonl"))
        ctx = _context()

        entry = journal.record(ctx, _no_trade_decision(), {"status": "NO_TRADE"})

        assert entry["strategy_version"] == "1.0.0"
        assert entry["strategy_status"] == "RESEARCH_ONLY"

    def test_entries_are_appended_as_valid_jsonl(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        journal = PaperForwardJournal(path=str(path))
        ctx = _context()

        journal.record(ctx, _no_trade_decision(), {"status": "NO_TRADE"})
        journal.record(ctx, _no_trade_decision(), {"status": "NO_TRADE"})

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must not raise

    def test_journal_never_raises_even_on_internal_error(self, tmp_path, monkeypatch):
        journal = PaperForwardJournal(path=str(tmp_path / "journal.jsonl"))

        class BrokenContext:
            @property
            def strategy_signals(self):
                raise RuntimeError("simulated internal failure")

        entry = journal.record(BrokenContext(), _no_trade_decision(), {})  # must not raise
        assert "journal_error" in entry

    def test_read_all_skips_corrupted_lines(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        path.write_text('{"a": 1}\nnot valid json\n{"b": 2}\n', encoding="utf-8")

        journal = PaperForwardJournal(path=str(path))
        entries = journal.read_all()

        assert entries == [{"a": 1}, {"b": 2}]

    def test_read_all_on_missing_file_returns_empty_list(self, tmp_path):
        journal = PaperForwardJournal(path=str(tmp_path / "does_not_exist.jsonl"))
        assert journal.read_all() == []


class TestNoTradeCandidatesAreJournaled:
    """Explicit requirement: every NO_TRADE and TRADE candidate is journaled, not just fills."""

    def test_no_trade_decisions_produce_a_journal_entry(self, tmp_path):
        journal = PaperForwardJournal(path=str(tmp_path / "journal.jsonl"))
        ctx = _context()

        journal.record(ctx, _no_trade_decision(), {"status": "NO_TRADE"})

        entries = journal.read_all()
        assert len(entries) == 1
        assert entries[0]["decision"] == "NO_TRADE"
