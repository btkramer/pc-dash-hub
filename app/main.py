import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

load_dotenv()

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_LIGHT_ENTITY = os.environ.get("HA_LIGHT_ENTITY", "light.office_desklight")

if not HA_URL or not HA_TOKEN:
    raise RuntimeError("HA_URL and HA_TOKEN must be set (see .env.example)")

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEVICES_DIR = DATA_DIR / "devices"

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "timeouts": {"volumeFuseMs": 5000, "idleToHomeMs": 30000},
    "weather": {"zip": "", "units": "F", "apiKey": ""},
    "clockFaceId": None,
    "buzzerEnabled": True,
    "vibrationEnabled": True,
    "macros": [],
    "activeProfile": "default",
}

app = FastAPI(title="pc-dash-hub")


class LightRequest(BaseModel):
    on: bool | None = None
    brightness: int | None = Field(default=None, ge=0, le=255)
    color_temp_kelvin: int | None = Field(default=None, ge=2700, le=6500)

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.on is None and self.brightness is None and self.color_temp_kelvin is None:
            raise ValueError("provide at least one of: on, brightness, color_temp_kelvin")
        return self


class TimeoutsUpdate(BaseModel):
    volumeFuseMs: int | None = None
    idleToHomeMs: int | None = None


class WeatherUpdate(BaseModel):
    zip: str | None = None
    units: str | None = None
    apiKey: str | None = None


class SettingsUpdate(BaseModel):
    timeouts: TimeoutsUpdate | None = None
    weather: WeatherUpdate | None = None
    clockFaceId: str | None = None
    buzzerEnabled: bool | None = None
    vibrationEnabled: bool | None = None
    macros: list[Any] | None = None
    activeProfile: str | None = None


def _device_path(device_id: str) -> Path:
    if not DEVICE_ID_RE.match(device_id):
        raise HTTPException(status_code=400, detail="invalid device_id")
    return DEVICES_DIR / f"{device_id}.json"


def _deep_merge(base: dict, updates: dict) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _load_settings(device_id: str) -> dict:
    path = _device_path(device_id)
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # cheap deep copy
    if path.exists():
        _deep_merge(settings, json.loads(path.read_text()))
    return settings


def _save_settings(device_id: str, settings: dict) -> None:
    path = _device_path(device_id)
    DEVICES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2))


def _device_state(device_id: str, settings: dict) -> dict:
    return {
        "deviceId": device_id,
        "settings": settings,
        "pcState": {
            "pcConnected": False,
            "lastSeenSecondsAgo": None,
        },
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/light")
async def set_light(req: LightRequest):
    service = "turn_off" if req.on is False else "turn_on"
    data = {"entity_id": HA_LIGHT_ENTITY}
    if service == "turn_on":
        if req.brightness is not None:
            data["brightness"] = req.brightness
        if req.color_temp_kelvin is not None:
            data["color_temp_kelvin"] = req.color_temp_kelvin

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{HA_URL}/api/services/light/{service}",
                headers={
                    "Authorization": f"Bearer {HA_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=data,
                timeout=10,
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"could not reach Home Assistant: {exc}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Home Assistant error: {resp.status_code} {resp.text}")

    return {"ok": True, "service": service, "data": data}


@app.get("/api/devices/{device_id}/state")
async def get_device_state(device_id: str):
    return _device_state(device_id, _load_settings(device_id))


@app.put("/api/devices/{device_id}/state")
async def update_device_state(device_id: str, update: SettingsUpdate):
    settings = _load_settings(device_id)
    _deep_merge(settings, update.model_dump(exclude_unset=True))
    _save_settings(device_id, settings)
    return _device_state(device_id, settings)
