# PC Dash Hub

A small self-hosted FastAPI service, run in Docker, that sits at the center of the PC Dash
ecosystem. The knob, the Windows app and any browser talk to the hub instead of to each other:
it stores and merges settings, relays live PC state with server-computed staleness, tracks
Claude Code agent sessions and passes smart-home actions to Home Assistant.

Part of the **PC Dash** family:

| Repo | Role |
|---|---|
| [lilygo-rotary-knob](https://github.com/btkramer/lilygo-rotary-knob) | The knob: ESP32-S3 firmware with a round AMOLED display |
| **pc-dash-hub** (this repo) | Self-hosted hub |
| [pc-dash-app](https://github.com/btkramer/pc-dash-app) | Windows companion app that reports real PC audio, media and agent state |

```
 ┌──────────┐   PUT pcState    ┌──────────────┐   GET state    ┌──────────┐
 │ Windows  │ ───────────────▶ │  pc-dash-hub │ ◀───────────── │   Knob   │
 │   app    │   agent events   │  (FastAPI)   │   settings     │ (ESP32)  │
 └──────────┘                  └──────┬───────┘                └──────────┘
                                      │ REST
                                      ▼
                               Home Assistant
```

## API

| Endpoint | Purpose |
|---|---|
| `GET/PUT /api/devices/{id}/state` | Per-device settings (deep-merged on PUT) plus the relayed `pcState`, with `pcConnected` and staleness computed by the server |
| `GET/PUT /api/settings` | Shared settings document |
| `GET /api/pc-status` | Is the companion app alive, and what is it reporting |
| `GET/PUT/DELETE /api/agent-sessions[/{id}]` | Claude Code session status feed for agent-monitor mode |
| `GET/POST /api/logos`, `GET/PUT /api/logo-assignments/{key}` | Project-logo uploads and assignments |
| `GET /api/generic-icons[/{name}]` | Built-in icon set |
| `GET/POST /light` | Home Assistant light passthrough |
| `GET /` | Browser control page (macro grid) |
| `GET /health` | Health check |

## Running

```sh
cp .env.example .env    # set HA_URL, HA_TOKEN, HA_LIGHT_ENTITY
docker build -t pc-dash-hub .
docker run -d --name pc-dash-hub -p 8015:8000 --env-file .env \
  -v pc-dash-hub-data:/app/data pc-dash-hub
```

Settings and logos persist under `DATA_DIR` (default `data/`). Use a long-lived Home Assistant
token from a dedicated, least-privilege user.
