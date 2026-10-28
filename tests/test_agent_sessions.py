import json
from datetime import datetime, timedelta, timezone

from app import main


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _write_index(data_dir, index):
    d = data_dir / "agent-sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(json.dumps(index))


def test_empty_list(client):
    assert client.get("/api/agent-sessions").json() == []


def test_create_stamps_times(client):
    r = client.put("/api/agent-sessions/s1", json={"agentProfile": "claude-code", "status": "working", "project": "hub"})
    assert r.status_code == 200
    s = r.json()
    assert s["sessionId"] == "s1"
    assert s["status"] == "working"
    assert s["project"] == "hub"
    assert s["startedAt"] == s["updatedAt"]
    assert s["stoppedAt"] is None
    assert s["stale"] is False
    assert s["lastUpdateSecondsAgo"] == 0


def test_client_supplied_started_at_kept(client):
    s = client.put("/api/agent-sessions/s1", json={"startedAt": "2026-01-01T00:00:00+00:00"}).json()
    assert s["startedAt"] == "2026-01-01T00:00:00+00:00"


def test_update_merges_and_list_get(client):
    client.put("/api/agent-sessions/s1", json={"status": "working", "project": "hub"})
    client.put("/api/agent-sessions/s2", json={"status": "needs-input"})
    first_started = client.get("/api/agent-sessions/s1").json()["startedAt"]

    s = client.put("/api/agent-sessions/s1", json={"status": "needs-input"}).json()
    assert s["project"] == "hub"
    assert s["startedAt"] == first_started

    ids = sorted(x["sessionId"] for x in client.get("/api/agent-sessions").json())
    assert ids == ["s1", "s2"]
    assert client.get("/api/agent-sessions/s2").json()["status"] == "needs-input"


def test_explicit_null_clears_field(client):
    client.put("/api/agent-sessions/s1", json={"project": "hub"})
    assert client.put("/api/agent-sessions/s1", json={"project": None}).json()["project"] is None


def test_working_to_idle_becomes_done(client):
    client.put("/api/agent-sessions/s1", json={"status": "working"})
    s = client.put("/api/agent-sessions/s1", json={"status": "idle"}).json()
    assert s["status"] == "done"
    assert s["stoppedAt"] is not None


def test_idle_to_idle_stays_idle(client):
    client.put("/api/agent-sessions/s1", json={"status": "idle"})
    s = client.put("/api/agent-sessions/s1", json={"status": "idle"}).json()
    assert s["status"] == "idle"
    assert s["stoppedAt"] is None


def test_done_put_stamps_and_repeat_preserves_stopped_at(client, isolated_data):
    client.put("/api/agent-sessions/s1", json={"status": "working"})
    first = client.put("/api/agent-sessions/s1", json={"status": "done"}).json()
    assert first["status"] == "done"
    assert first["stoppedAt"] is not None

    # Back-to-back done keeps the original timestamp.
    index = json.loads((isolated_data / "agent-sessions" / "index.json").read_text())
    index["s1"]["stoppedAt"] = "2026-01-01T00:00:00+00:00"
    _write_index(isolated_data, index)
    again = client.put("/api/agent-sessions/s1", json={"status": "done"}).json()
    assert again["stoppedAt"] == "2026-01-01T00:00:00+00:00"


def test_active_again_clears_stopped_at(client):
    for status in ("working", "needs-input", "error"):
        client.put("/api/agent-sessions/s1", json={"status": "done"})
        s = client.put("/api/agent-sessions/s1", json={"status": status}).json()
        assert s["status"] == status
        assert s["stoppedAt"] is None


def test_done_decays_to_idle(client, isolated_data):
    _write_index(
        isolated_data,
        {
            "recent": {"status": "done", "stoppedAt": _iso_ago(1), "updatedAt": _iso_ago(1)},
            "old": {"status": "done", "stoppedAt": _iso_ago(main.AGENT_SESSION_DONE_DECAY_SECONDS + 1), "updatedAt": _iso_ago(1)},
            "legacy": {"status": "done", "stoppedAt": None, "updatedAt": _iso_ago(1)},
        },
    )
    by_id = {s["sessionId"]: s for s in client.get("/api/agent-sessions").json()}
    assert by_id["recent"]["status"] == "done"
    assert by_id["old"]["status"] == "idle"
    assert by_id["old"]["stoppedAt"] is not None
    assert by_id["legacy"]["status"] == "idle"


def test_staleness(client, isolated_data):
    age = main.AGENT_SESSION_STALE_AFTER_SECONDS + 10
    _write_index(isolated_data, {"s1": {"status": "working", "updatedAt": _iso_ago(age)}, "s2": {"status": "idle"}})
    by_id = {s["sessionId"]: s for s in client.get("/api/agent-sessions").json()}
    assert by_id["s1"]["stale"] is True
    assert by_id["s1"]["lastUpdateSecondsAgo"] == age
    assert by_id["s2"]["stale"] is True
    assert by_id["s2"]["lastUpdateSecondsAgo"] is None


def test_delete(client):
    client.put("/api/agent-sessions/s1", json={"status": "working"})
    assert client.delete("/api/agent-sessions/s1").json() == {"ok": True}
    assert client.get("/api/agent-sessions/s1").status_code == 404
    assert client.get("/api/agent-sessions").json() == []


def test_404s(client):
    assert client.get("/api/agent-sessions/missing").status_code == 404
    assert client.delete("/api/agent-sessions/missing").status_code == 404


def test_validation(client):
    assert client.put("/api/agent-sessions/s1", json={"status": "sleeping"}).status_code == 422
    # updatedAt and stoppedAt are server-managed.
    assert client.put("/api/agent-sessions/s1", json={"updatedAt": "x"}).status_code == 422
    assert client.put("/api/agent-sessions/s1", json={"stoppedAt": "x"}).status_code == 422


def test_invalid_session_id(client):
    assert client.get("/api/agent-sessions/bad.id").status_code == 400
    assert client.put("/api/agent-sessions/bad$id", json={}).status_code == 400
    assert client.delete("/api/agent-sessions/bad id").status_code == 400
