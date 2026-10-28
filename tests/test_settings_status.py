import json
from datetime import datetime, timedelta, timezone

from app import main


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_global_settings_defaults(client):
    assert client.get("/api/settings").json() == main.DEFAULT_GLOBAL_SETTINGS


def test_global_settings_field_merge(client):
    client.put("/api/settings", json={"attentionRenagMinutes": 0})
    r = client.put("/api/settings", json={"phoneBeepEnabled": False})
    assert r.json() == {"attentionRenagMinutes": 0, "knobBeepEnabled": True, "phoneBeepEnabled": False}
    assert client.get("/api/settings").json() == r.json()


def test_global_settings_write_is_atomic(client, isolated_data):
    client.put("/api/settings", json={"knobBeepEnabled": False})
    assert json.loads((isolated_data / "settings.json").read_text()) == {"knobBeepEnabled": False}
    assert not (isolated_data / "settings.json.tmp").exists()


def test_global_settings_validation(client):
    assert client.put("/api/settings", json={"attentionRenagMinutes": -1}).status_code == 422
    assert client.put("/api/settings", json={"nope": 1}).status_code == 422


def test_pc_status_without_reports(client):
    assert client.get("/api/pc-status").json() == {"pcConnected": False, "lastSeenSecondsAgo": None}


def test_pc_status_after_report(client):
    client.put("/api/devices/knob1/state", json={"pcState": {"volumePercent": 5}})
    assert client.get("/api/pc-status").json() == {"pcConnected": True, "lastSeenSecondsAgo": 0}


def test_pc_status_uses_most_recent_device(client, isolated_data):
    devices = isolated_data / "devices"
    devices.mkdir(parents=True)
    for name, age in (("old", 600), ("recent", 2), ("never", None)):
        doc = {"settings": {}, "pcState": {}, "macroState": {}, "lastSeenAt": None if age is None else _iso_ago(age)}
        (devices / f"{name}.json").write_text(json.dumps(doc))
    (devices / "corrupt.json").write_text("{not json")
    assert client.get("/api/pc-status").json() == {"pcConnected": True, "lastSeenSecondsAgo": 2}


def test_pc_status_stale(client, isolated_data):
    devices = isolated_data / "devices"
    devices.mkdir(parents=True)
    age = main.PC_STATE_STALE_AFTER_SECONDS + 1
    doc = {"settings": {}, "pcState": {}, "macroState": {}, "lastSeenAt": _iso_ago(age)}
    (devices / "knob1.json").write_text(json.dumps(doc))
    assert client.get("/api/pc-status").json() == {"pcConnected": False, "lastSeenSecondsAgo": age}
