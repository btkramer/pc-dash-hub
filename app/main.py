import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

load_dotenv()

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_LIGHT_ENTITY = os.environ.get("HA_LIGHT_ENTITY", "light.office_desklight")

if not HA_URL or not HA_TOKEN:
    raise RuntimeError("HA_URL and HA_TOKEN must be set (see .env.example)")

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEVICES_DIR = DATA_DIR / "devices"
LOGOS_DIR = DATA_DIR / "logos"
LOGOS_INDEX_PATH = LOGOS_DIR / "index.json"

LOGO_ID_RE = re.compile(r"^[0-9a-f]{16}$")
LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2MB -- generous for an icon/logo image

PC_STATE_STALE_AFTER_SECONDS = int(os.environ.get("PC_STATE_STALE_AFTER_SECONDS", "12"))

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_SETTINGS: dict[str, Any] = {
    "timeouts": {"volumeFuseMs": 5000, "idleToHomeMs": 30000, "dimDelayMs": 60000},
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
    "utcOffsetMinutes": None,
    "timeZoneName": None,
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


class StrictModel(BaseModel):
    # T31: a misshapen request body (e.g. a stray wrapper key) otherwise
    # validates fine and silently drops every real field instead of
    # erroring -- reject unrecognized fields instead of ignoring them.
    model_config = ConfigDict(extra="forbid")


class TimeoutsUpdate(StrictModel):
    volumeFuseMs: int | None = None
    idleToHomeMs: int | None = None
    dimDelayMs: int | None = None


class WeatherUpdate(StrictModel):
    zip: str | None = None
    units: str | None = None
    apiKey: str | None = None


class NowPlayingUpdate(StrictModel):
    title: str | None = None
    artist: str | None = None
    source: str | None = None
    positionMs: int | None = None
    durationMs: int | None = None


class PcStateUpdate(StrictModel):
    volumePercent: int | None = Field(default=None, ge=0, le=100)
    speakerMuted: bool | None = None
    nowPlayingIsPlaying: bool | None = None
    nowPlaying: NowPlayingUpdate | None = None
    # T44: signed minutes east of UTC (e.g. -420 for Phoenix) — offset-based
    # rather than a POSIX TZ string so neither this hub nor the knob has to
    # implement DST rules; pc-dash-app self-corrects it on every report.
    utcOffsetMinutes: int | None = None
    # Windows' Standard/Daylight display name -- cosmetic only, not parsed.
    timeZoneName: str | None = None


class MacroDefinition(BaseModel):
    id: str
    label: str
    icon: str | None = None
    type: str
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SettingsUpdate(StrictModel):
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


def _load_logo_index() -> dict[str, dict]:
    if not LOGOS_INDEX_PATH.exists():
        return {}
    return json.loads(LOGOS_INDEX_PATH.read_text())


def _save_logo_index(index: dict[str, dict]) -> None:
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    LOGOS_INDEX_PATH.write_text(json.dumps(index, indent=2))


def _logo_blob_path(logo_id: str) -> Path:
    return LOGOS_DIR / f"{logo_id}.bin"


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


@app.get("/")
async def deck_ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/light")
async def get_light():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{HA_URL}/api/states/{HA_LIGHT_ENTITY}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=10,
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"could not reach Home Assistant: {exc}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Home Assistant error: {resp.status_code} {resp.text}")

    data = resp.json()
    attrs = data.get("attributes", {})
    return {
        "on": data.get("state") == "on",
        "brightness": attrs.get("brightness"),
        "color_temp_kelvin": attrs.get("color_temp_kelvin"),
    }


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


@app.post("/api/logos")
async def upload_logo(file: UploadFile):
    # T52: shared cross-project logo library. id = first 16 hex chars of the
    # content's SHA-256, so identical uploads from any participant (or a
    # re-upload of the same file) dedup onto the same id automatically --
    # no separate "does this already exist" check needed by the caller.
    body = await file.read()
    if len(body) > LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"logo exceeds {LOGO_MAX_BYTES} bytes")
    if not body:
        raise HTTPException(status_code=400, detail="empty upload")

    logo_id = hashlib.sha256(body).hexdigest()[:16]
    index = _load_logo_index()

    if logo_id not in index:
        LOGOS_DIR.mkdir(parents=True, exist_ok=True)
        _logo_blob_path(logo_id).write_bytes(body)
        index[logo_id] = {
            "id": logo_id,
            "contentType": file.content_type or "application/octet-stream",
            "sizeBytes": len(body),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        _save_logo_index(index)

    return index[logo_id]


@app.get("/api/logos")
async def list_logos():
    return list(_load_logo_index().values())


@app.get("/api/logos/{logo_id}")
async def get_logo(logo_id: str):
    if not LOGO_ID_RE.match(logo_id):
        raise HTTPException(status_code=400, detail="invalid logo id")
    entry = _load_logo_index().get(logo_id)
    blob_path = _logo_blob_path(logo_id)
    if entry is None or not blob_path.exists():
        raise HTTPException(status_code=404, detail="logo not found")
    return Response(content=blob_path.read_bytes(), media_type=entry["contentType"])


@app.get("/api/devices/{device_id}/state")
async def get_device_state(device_id: str):
    doc = _load_device_doc(device_id)
    settings = _with_defaults(DEFAULT_SETTINGS, doc["settings"])
    pc_state = _with_defaults(DEFAULT_PC_STATE, doc["pcState"])
    macro_state = _with_defaults(DEFAULT_MACRO_STATE, doc["macroState"])
    # Temporary T25 debug instrumentation -- remove once the knob's
    # position/play-state lag is root-caused. See coordinator-reports/pc-dash.md.
    now_playing = pc_state.get("nowPlaying") or {}
    print(
        f"PCSTATE GET device={device_id} isPlaying={pc_state.get('nowPlayingIsPlaying')} "
        f"positionMs={now_playing.get('positionMs')} "
        f"volumePercent={pc_state.get('volumePercent')} speakerMuted={pc_state.get('speakerMuted')}",
        flush=True,
    )
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
        # Temporary T25 debug instrumentation -- remove once the knob's
        # position/play-state lag is root-caused. See coordinator-reports/pc-dash.md.
        is_playing_in = pc_update.get("nowPlayingIsPlaying", "n/a")
        now_playing_in = pc_update.get("nowPlaying")
        position_in = now_playing_in.get("positionMs", "n/a") if now_playing_in is not None else "n/a"
        volume_in = pc_update.get("volumePercent", "n/a")
        muted_in = pc_update.get("speakerMuted", "n/a")
        print(
            f"PCSTATE PUT device={device_id} isPlaying={is_playing_in} positionMs={position_in} "
            f"volumePercent={volume_in} speakerMuted={muted_in}",
            flush=True,
        )
        _deep_merge(pc_state, pc_update)
        last_seen_at = datetime.now(timezone.utc).isoformat()

    macro_state = _with_defaults(DEFAULT_MACRO_STATE, doc["macroState"])
    if update.macroState is not None:
        _deep_merge(macro_state, update.macroState)

    _save_device_doc(device_id, settings, pc_state, macro_state, last_seen_at)
    return _device_state(device_id, settings, pc_state, macro_state, last_seen_at)
