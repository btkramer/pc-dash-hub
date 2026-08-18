import os

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
