import pytest

from app import main


def test_off_by_default(client):
    assert main.HUB_TOKEN == ""
    assert client.get("/api/settings").status_code == 200
    assert client.put("/api/agent-sessions/s1", json={"status": "idle"}).status_code == 200


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setattr(main, "HUB_TOKEN", "s3cret")


def test_required_when_set(client, with_token):
    r = client.get("/api/settings")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/settings", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/settings", headers={"Authorization": "s3cret"}).status_code == 401
    assert client.put("/api/devices/knob1/state", json={}).status_code == 401


def test_correct_token_accepted(client, with_token):
    auth = {"Authorization": "Bearer s3cret"}
    assert client.get("/api/settings", headers=auth).status_code == 200
    assert client.put("/api/devices/knob1/state", json={"buzzerEnabled": False}, headers=auth).status_code == 200


def test_rejected_request_changes_nothing(client, with_token, isolated_data):
    client.put("/api/settings", json={"knobBeepEnabled": False})
    assert not (isolated_data / "settings.json").exists()


def test_health_stays_open(client, with_token):
    assert client.get("/health").json() == {"ok": True}
