import json

import httpx


def test_get_light(client, fake_ha):
    assert client.get("/light").json() == {"on": True, "brightness": 128, "color_temp_kelvin": 3000}
    (req,) = fake_ha.requests
    assert str(req.url) == "http://ha.test:8123/api/states/light.test_lamp"
    assert req.headers["authorization"] == "Bearer test-token"


def test_turn_on_with_values(client, fake_ha):
    r = client.post("/light", json={"on": True, "brightness": 200, "color_temp_kelvin": 4000})
    assert r.json() == {
        "ok": True,
        "service": "turn_on",
        "data": {"entity_id": "light.test_lamp", "brightness": 200, "color_temp_kelvin": 4000},
    }
    (req,) = fake_ha.requests
    assert req.url.path == "/api/services/light/turn_on"
    assert json.loads(req.content) == r.json()["data"]


def test_brightness_alone_turns_on(client, fake_ha):
    assert client.post("/light", json={"brightness": 10}).json()["service"] == "turn_on"


def test_turn_off_ignores_values(client, fake_ha):
    r = client.post("/light", json={"on": False, "brightness": 50})
    assert r.json() == {"ok": True, "service": "turn_off", "data": {"entity_id": "light.test_lamp"}}
    assert fake_ha.requests[0].url.path == "/api/services/light/turn_off"


def test_light_validation(client, fake_ha):
    assert client.post("/light", json={}).status_code == 422
    assert client.post("/light", json={"brightness": 256}).status_code == 422
    assert client.post("/light", json={"color_temp_kelvin": 2000}).status_code == 422
    assert fake_ha.requests == []


def test_ha_unreachable(client, fake_ha):
    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)

    fake_ha.handler = unreachable
    r = client.get("/light")
    assert r.status_code == 502
    assert "could not reach Home Assistant" in r.json()["detail"]
    r = client.post("/light", json={"on": True})
    assert r.status_code == 502
    assert "could not reach Home Assistant" in r.json()["detail"]


def test_ha_timeout(client, fake_ha):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    fake_ha.handler = slow
    assert client.get("/light").status_code == 502


def test_ha_error_status(client, fake_ha):
    fake_ha.handler = lambda request: httpx.Response(401, text="Unauthorized")
    r = client.get("/light")
    assert r.status_code == 502
    assert r.json()["detail"] == "Home Assistant error: 401 Unauthorized"

    fake_ha.handler = lambda request: httpx.Response(500, text="boom")
    r = client.post("/light", json={"on": False})
    assert r.status_code == 502
    assert r.json()["detail"] == "Home Assistant error: 500 boom"
