import json
from datetime import datetime, timedelta, timezone

from app import main


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_unknown_device_returns_defaults(client):
    body = client.get("/api/devices/knob1/state").json()
    assert body["deviceId"] == "knob1"
    assert body["settings"]["timeouts"] == main.DEFAULT_SETTINGS["timeouts"]
    assert body["settings"]["macros"] == []
    assert body["pcState"]["volumePercent"] is None
    assert body["pcState"]["pcConnected"] is False
    assert body["pcState"]["lastSeenSecondsAgo"] is None
    assert body["macroState"] == {}


def test_get_does_not_create_a_file(client, isolated_data):
    client.get("/api/devices/knob1/state")
    assert not (isolated_data / "devices").exists()


def test_settings_deep_merge(client):
    client.put("/api/devices/knob1/state", json={"timeouts": {"volumeFuseMs": 1234}})
    r = client.put("/api/devices/knob1/state", json={"timeouts": {"dimDelayMs": 9}, "weather": {"zip": "85001"}})
    s = r.json()["settings"]
    # Earlier partial update survives; untouched defaults are still there.
    assert s["timeouts"] == {"volumeFuseMs": 1234, "idleToHomeMs": 30000, "dimDelayMs": 9}
    assert s["weather"] == {"zip": "85001", "units": "F", "apiKey": ""}
    assert client.get("/api/devices/knob1/state").json()["settings"]["timeouts"]["volumeFuseMs"] == 1234


def test_macros_replace_wholesale(client):
    m1 = {"id": "a", "label": "A", "type": "key", "target": "pc"}
    m2 = {"id": "b", "label": "B", "type": "key", "target": "pc"}
    client.put("/api/devices/knob1/state", json={"macros": [m1, m2]})
    r = client.put("/api/devices/knob1/state", json={"macros": [m2]})
    assert [m["id"] for m in r.json()["settings"]["macros"]] == ["b"]


def test_pc_state_deep_merge_and_explicit_null(client):
    client.put(
        "/api/devices/knob1/state",
        json={"pcState": {"volumePercent": 40, "nowPlaying": {"title": "Song", "artist": "Band"}}},
    )
    r = client.put("/api/devices/knob1/state", json={"pcState": {"nowPlaying": {"positionMs": 5000}}})
    pc = r.json()["pcState"]
    assert pc["volumePercent"] == 40
    assert pc["nowPlaying"] == {"title": "Song", "artist": "Band", "positionMs": 5000}

    # Omitting a field keeps it; an explicit null clears it.
    r = client.put("/api/devices/knob1/state", json={"pcState": {"volumePercent": None}})
    assert r.json()["pcState"]["volumePercent"] is None
    assert r.json()["pcState"]["nowPlaying"]["title"] == "Song"


def test_pc_state_put_marks_connected(client):
    r = client.put("/api/devices/knob1/state", json={"pcState": {"speakerMuted": False}})
    pc = r.json()["pcState"]
    assert pc["pcConnected"] is True
    assert pc["lastSeenSecondsAgo"] == 0


def test_settings_only_put_does_not_mark_connected(client):
    r = client.put("/api/devices/knob1/state", json={"buzzerEnabled": False})
    assert r.json()["pcState"]["pcConnected"] is False


def test_pc_state_goes_stale(client, isolated_data):
    devices = isolated_data / "devices"
    devices.mkdir(parents=True)
    age = main.PC_STATE_STALE_AFTER_SECONDS + 5
    doc = {"settings": {}, "pcState": {"volumePercent": 10}, "macroState": {}, "lastSeenAt": _iso_ago(age)}
    (devices / "knob1.json").write_text(json.dumps(doc))

    pc = client.get("/api/devices/knob1/state").json()["pcState"]
    assert pc["pcConnected"] is False
    assert pc["lastSeenSecondsAgo"] == age
    assert pc["volumePercent"] == 10


def test_pc_state_just_inside_threshold_is_connected(client, isolated_data):
    devices = isolated_data / "devices"
    devices.mkdir(parents=True)
    doc = {"settings": {}, "pcState": {}, "macroState": {}, "lastSeenAt": _iso_ago(main.PC_STATE_STALE_AFTER_SECONDS - 3)}
    (devices / "knob1.json").write_text(json.dumps(doc))
    assert client.get("/api/devices/knob1/state").json()["pcState"]["pcConnected"] is True


def test_macro_state_merges_per_id(client):
    client.put("/api/devices/knob1/state", json={"macroState": {"shot": {"mode": "window"}, "other": {"x": 1}}})
    r = client.put("/api/devices/knob1/state", json={"macroState": {"shot": {"count": 2}}})
    assert r.json()["macroState"] == {"shot": {"mode": "window", "count": 2}, "other": {"x": 1}}


def test_legacy_settings_only_file_is_read(client, isolated_data):
    devices = isolated_data / "devices"
    devices.mkdir(parents=True)
    (devices / "old.json").write_text(json.dumps({"clockFaceId": "analog"}))
    body = client.get("/api/devices/old/state").json()
    assert body["settings"]["clockFaceId"] == "analog"
    assert body["pcState"]["pcConnected"] is False


def test_state_persists_to_disk(client, isolated_data):
    client.put("/api/devices/knob1/state", json={"activeProfile": "work"})
    saved = json.loads((isolated_data / "devices" / "knob1.json").read_text())
    assert saved["settings"]["activeProfile"] == "work"
    assert set(saved) == {"settings", "pcState", "macroState", "lastSeenAt"}


def test_global_settings_echoed_and_buzzer_alias(client):
    client.put("/api/settings", json={"knobBeepEnabled": False, "attentionRenagMinutes": 7})
    client.put("/api/devices/knob1/state", json={"buzzerEnabled": True})
    s = client.get("/api/devices/knob1/state").json()["settings"]
    assert s["attentionRenagMinutes"] == 7
    assert s["knobBeepEnabled"] is False
    # The global knobBeepEnabled wins over the stored per-device buzzerEnabled.
    assert s["buzzerEnabled"] is False


def test_unknown_field_rejected(client):
    r = client.put("/api/devices/knob1/state", json={"settings": {"buzzerEnabled": False}})
    assert r.status_code == 422
    r = client.put("/api/devices/knob1/state", json={"pcState": {"volume": 5}})
    assert r.status_code == 422


def test_volume_out_of_range_rejected(client):
    assert client.put("/api/devices/knob1/state", json={"pcState": {"volumePercent": 101}}).status_code == 422


def test_invalid_device_id(client):
    assert client.get("/api/devices/bad.id/state").status_code == 400
    assert client.put("/api/devices/bad$id/state", json={}).status_code == 400
    assert client.get("/api/devices/..%2F..%2Fetc/state").status_code in (400, 404)
