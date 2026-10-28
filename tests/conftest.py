import os
import socket
from pathlib import Path

import httpx
import pytest

# app.main reads its config at import time and refuses to start without HA_URL/HA_TOKEN, so set
# test values before it is imported. A real .env never overrides these (load_dotenv doesn't
# replace variables that are already set).
os.environ["HA_URL"] = "http://ha.test:8123"
os.environ["HA_TOKEN"] = "test-token"
os.environ["HA_LIGHT_ENTITY"] = "light.test_lamp"
os.environ.pop("HUB_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch) -> Path:
    """Point every storage path at a fresh temp dir, so tests never see or touch real data."""
    data = tmp_path / "data"
    monkeypatch.setattr(main, "DATA_DIR", data)
    monkeypatch.setattr(main, "DEVICES_DIR", data / "devices")
    monkeypatch.setattr(main, "LOGOS_DIR", data / "logos")
    monkeypatch.setattr(main, "LOGOS_INDEX_PATH", data / "logos" / "index.json")
    monkeypatch.setattr(main, "LOGO_ASSIGNMENTS_DIR", data / "logo-assignments")
    monkeypatch.setattr(main, "AGENT_SESSIONS_DIR", data / "agent-sessions")
    monkeypatch.setattr(main, "AGENT_SESSIONS_INDEX_PATH", data / "agent-sessions" / "index.json")
    monkeypatch.setattr(main, "GLOBAL_SETTINGS_PATH", data / "settings.json")
    return data


class FakeHomeAssistant:
    """Answers the hub's Home Assistant calls in memory and records them."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = self.default_handler

    def default_handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states/light.test_lamp":
            return httpx.Response(
                200,
                json={"state": "on", "attributes": {"brightness": 128, "color_temp_kelvin": 3000}},
            )
        if request.method == "POST" and request.url.path.startswith("/api/services/light/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="not mocked")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture(autouse=True)
def fake_ha(monkeypatch) -> FakeHomeAssistant:
    """Route every httpx.AsyncClient through an in-memory transport: no test can reach the network."""
    fake = FakeHomeAssistant()

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", client_factory)
    return fake


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if anything opens a real connection. TestClient runs the app in-process."""

    def blocked(*args, **kwargs):
        raise RuntimeError("tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def client() -> TestClient:
    with TestClient(main.app) as c:
        yield c
