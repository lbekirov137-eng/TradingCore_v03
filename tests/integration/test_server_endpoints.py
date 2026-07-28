from fastapi.testclient import TestClient

from api.server import app
from api.data_engine import DataEngine

from tests.conftest import orb_breakout_snapshot


client = TestClient(app)


def test_root_online():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "online"


def test_safety_endpoint_reflects_safe_defaults_without_leaking_secrets():
    r = client.get("/safety")
    assert r.status_code == 200
    body = r.json()

    assert body["paper_trading"] is True
    assert body["live_trading"] is False
    assert body["live_order_code_present"] is False


def test_paper_tick_endpoint_never_500s_on_exchange_failure(monkeypatch):
    def _raise(**kw):
        raise ConnectionError("simulated outage")

    monkeypatch.setattr(DataEngine, "load", staticmethod(_raise))

    r = client.get("/paper/tick")

    assert r.status_code == 200
    assert r.json()["decision"]["decision"] == "NO_TRADE"


def test_paper_tick_endpoint_happy_path(monkeypatch):
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    # replay=true: фикстура использует детерминированные исторические
    # timestamps. Без этого флага stale-фильтр (правильно) отклонил бы их.
    r = client.get("/paper/tick", params={"replay": "true"})

    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["decision"] == "TRADE"
    assert body["execution"]["status"] == "OPENED"


def test_analyze_endpoint_never_500s_on_exchange_failure(monkeypatch):
    def _raise(**kw):
        raise ConnectionError("simulated outage")

    monkeypatch.setattr(DataEngine, "load", staticmethod(_raise))

    r = client.get("/analyze")

    assert r.status_code == 200
    assert r.json()["decision"]["decision"] == "NO_TRADE"


def test_kill_switch_engage_status_disengage_roundtrip():
    r = client.get("/kill-switch/status")
    assert r.status_code == 200
    assert r.json()["engaged"] is False

    r = client.post("/kill-switch/engage", params={"reason": "test", "operator": "qa"})
    assert r.status_code == 200
    assert r.json()["engaged"] is True

    r = client.get("/safety")
    assert r.json()["kill_switch_engaged"] is True

    r = client.post("/kill-switch/disengage", params={"operator": "qa"})
    assert r.status_code == 200
    assert r.json()["engaged"] is False


def test_stale_data_is_rejected_without_replay_flag(monkeypatch):
    """
    Без replay=true исторические (2024) данные обязаны быть отклонены
    как устаревшие — это защита от залипшего фида в реальном запуске.
    """
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    r = client.get("/paper/tick")

    assert r.status_code == 200
    assert r.json()["decision"]["decision"] == "NO_TRADE"


def test_kill_switch_blocks_paper_tick_trade(monkeypatch):
    monkeypatch.setattr(DataEngine, "load", staticmethod(lambda **kw: orb_breakout_snapshot(breakout=True)))

    client.post("/kill-switch/engage", params={"reason": "blocking test"})

    r = client.get("/paper/tick", params={"replay": "true"})

    assert r.status_code == 200
    assert r.json()["decision"]["decision"] == "NO_TRADE"

    client.post("/kill-switch/disengage")
