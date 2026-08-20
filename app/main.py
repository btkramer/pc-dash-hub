import json
import os
import re
from datetime import datetime, timezone
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

PC_STATE_STALE_AFTER_SECONDS = int(os.environ.get("PC_STATE_STALE_AFTER_SECONDS", "12"))

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

DEFAULT_PC_STATE: dict[str, Any] = {
    "volumePercent": None,
    "speakerMuted": None,
    "nowPlayingIsPlaying": None,
    "nowPlaying": None,
}

# Mutable per-macro runtime state (e.g. screenshot.capture's live capture
# mode), keyed by macro id. Deliberately separate from `macros` itself
# (see MacroDefinition/SettingsUpdate below) since macro *definitions*
# replace wholesale on PUT, but this needs per-id partial merge — see
# coordinator-reports/pc-dash.md's 2026-08-20 T8 entries for the full
# reasoning, worked out jointly with pc-dash-app.
DEFAULT_MACRO_STATE: dict[str, Any] = {}

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


class NowPlayingUpdate(BaseModel):
    title: str | None = None
    artist: str | None = None
    source: str | None = None
    positionMs: int | None = None
    durationMs: int | None = None


class PcStateUpdate(BaseModel):
    volumePercent: int | None = Field(default=None, ge=0, le=100)
    speakerMuted: bool | None = None
    nowPlayingIsPlaying: bool | None = None
    nowPlaying: NowPlayingUpdate | None = None


class MacroDefinition(BaseModel):
    id: str
    label: str
    icon: str | None = None
    type: str
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    timeouts: TimeoutsUpdate | None = None
    weather: WeatherUpdate | None = None
    clockFaceId: str | None = None
    buzzerEnabled: bool | None = None
    vibrationEnabled: bool | None = None
    macros: list[MacroDefinition] | None = None
    activeProfile: str | None = None
    pcState: PcStateUpdate | None = None
    macroState: dict[str, dict[str, Any]] | None = None


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


def _load_device_doc(device_id: str) -> dict:
    path = _device_path(device_id)
    if not path.exists():
        return {"settings": {}, "pcState": {}, "macroState": {}, "lastSeenAt": None}
    raw = json.loads(path.read_text())
    if "settings" in raw or "pcState" in raw:
        return {
            "settings": raw.get("settings", {}),
            "pcState": raw.get("pcState", {}),
            "macroState": raw.get("macroState", {}),
            "lastSeenAt": raw.get("lastSeenAt"),
        }
    # Pre-pcState file format: the whole file was just the settings dict.
    return {"settings": raw, "pcState": {}, "macroState": {}, "lastSeenAt": None}


def _with_defaults(defaults: dict, stored: dict) -> dict:
    merged = json.loads(json.dumps(defaults))  # cheap deep copy
    _deep_merge(merged, stored)
    return merged


def _save_device_doc(
    device_id: str, settings: dict, pc_state: dict, macro_state: dict, last_seen_at: str | None
) -> None:
    path = _device_path(device_id)
    DEVICES_DIR.mkdir(parents=True, exist_ok=True)
    doc = {"settings": settings, "pcState": pc_state, "macroState": macro_state, "lastSeenAt": last_seen_at}
    path.write_text(json.dumps(doc, indent=2))


def _device_state(device_id: str, settings: dict, pc_state: dict, macro_state: dict, last_seen_at: str | None) -> dict:
    if last_seen_at is None:
        seconds_ago = None
        connected = False
    else:
        seen = datetime.fromisoformat(last_seen_at)
        seconds_ago = (datetime.now(timezone.utc) - seen).total_seconds()
        connected = seconds_ago < PC_STATE_STALE_AFTER_SECONDS

    return {
        "deviceId": device_id,
        "settings": settings,
        "pcState": {
            **pc_state,
            "pcConnected": connected,
            "lastSeenSecondsAgo": None if seconds_ago is None else round(seconds_ago),
        },
        "macroState": macro_state,
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
    doc = _load_device_doc(device_id)
    settings = _with_defaults(DEFAULT_SETTINGS, doc["settings"])
    pc_state = _with_defaults(DEFAULT_PC_STATE, doc["pcState"])
    macro_state = _with_defaults(DEFAULT_MACRO_STATE, doc["macroState"])
    return _device_state(device_id, settings, pc_state, macro_state, doc["lastSeenAt"])


@app.put("/api/devices/{device_id}/state")
async def update_device_state(device_id: str, update: SettingsUpdate):
    doc = _load_device_doc(device_id)

    settings = _with_defaults(DEFAULT_SETTINGS, doc["settings"])
    update_fields = update.model_dump(exclude_unset=True, exclude={"pcState", "macroState"})
    _deep_merge(settings, update_fields)

    pc_state = _with_defaults(DEFAULT_PC_STATE, doc["pcState"])
    last_seen_at = doc["lastSeenAt"]
    if update.pcState is not None:
        pc_update = update.pcState.model_dump(exclude_unset=True)
        _deep_merge(pc_state, pc_update)
        last_seen_at = datetime.now(timezone.utc).isoformat()

    macro_state = _with_defaults(DEFAULT_MACRO_STATE, doc["macroState"])
    if update.macroState is not None:
        _deep_merge(macro_state, update.macroState)

    _save_device_doc(device_id, settings, pc_state, macro_state, last_seen_at)
    return _device_state(device_id, settings, pc_state, macro_state, last_seen_at)
