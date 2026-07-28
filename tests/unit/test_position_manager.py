import pytest

from api.position_manager.position_manager import PositionManager, DEFAULT_STATE_PATH


def _restore_default_state_path():
    PositionManager._state_path = DEFAULT_STATE_PATH
    PositionManager._loaded = False
    PositionManager.reset()


class TestPositionManager:
    """
    CRITICAL finding: до фикса has_open_position() был захардкожен на
    False — правило "одновременно только одна позиция" (TRADE_LIFECYCLE.md)
    не проверялось нигде и ничем не могло быть заблокировано.
    """

    def test_no_open_position_initially(self):
        assert PositionManager.has_open_position() is False
        assert PositionManager.current_position() is None

    def test_open_position_is_tracked(self):
        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="sig-1")
        assert PositionManager.has_open_position() is True
        assert PositionManager.current_position() == {"symbol": "BTCUSDT"}

    def test_cannot_open_second_position_while_one_open(self):
        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="sig-1")

        with pytest.raises(RuntimeError):
            PositionManager.open_position({"symbol": "ETHUSDT"}, signature="sig-2")

    def test_close_position_clears_state(self):
        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="sig-1")
        closed = PositionManager.close_position("test")

        assert closed == {"symbol": "BTCUSDT"}
        assert PositionManager.has_open_position() is False

    def test_duplicate_signature_detected_after_close(self):
        """Идемпотентность: тот же ордер не должен пройти дважды подряд."""
        PositionManager.open_position({"symbol": "BTCUSDT"}, signature="sig-1")
        PositionManager.close_position("tp")

        assert PositionManager.is_duplicate_signature("sig-1") is True
        assert PositionManager.is_duplicate_signature("sig-2") is False

    def test_duplicate_session_detected(self):
        PositionManager.open_position(
            {"symbol": "BTCUSDT"}, signature="sig-1", session_key="session-A"
        )
        PositionManager.close_position("tp")

        assert PositionManager.is_duplicate_session("session-A") is True
        assert PositionManager.is_duplicate_session("session-B") is False

    def test_reset_clears_everything(self):
        PositionManager.open_position(
            {"symbol": "BTCUSDT"}, signature="sig-1", session_key="session-A"
        )
        PositionManager.reset()

        assert PositionManager.has_open_position() is False
        assert PositionManager.is_duplicate_signature("sig-1") is False
        assert PositionManager.is_duplicate_session("session-A") is False

    def test_restart_recovery_reloads_open_position_from_disk(self, tmp_path):
        state_path = str(tmp_path / "position.json")
        PositionManager.reset(state_path=state_path)

        PositionManager.open_position(
            {"symbol": "BTCUSDT", "entry": 100.0},
            signature=("binance", "BTCUSDT", "LONG", 100.0, 98.0),
            session_key=("binance", "BTCUSDT", "ORB", "CRYPTO", 123),
        )

        # Simulate process restart: force a reload from disk.
        PositionManager._loaded = False

        assert PositionManager.has_open_position() is True
        assert PositionManager.current_position()["symbol"] == "BTCUSDT"
        assert PositionManager.is_duplicate_signature(("binance", "BTCUSDT", "LONG", 100.0, 98.0)) is True
        assert PositionManager.is_duplicate_session(("binance", "BTCUSDT", "ORB", "CRYPTO", 123)) is True

        _restore_default_state_path()

    def test_corrupted_position_state_file_does_not_crash(self, tmp_path):
        state_path = tmp_path / "position.json"
        state_path.write_text("{not valid json", encoding="utf-8")

        PositionManager._state_path = str(state_path)
        PositionManager._loaded = False

        assert PositionManager.has_open_position() is False  # fails safe, no crash

        _restore_default_state_path()
