# Rebuild Guide

## Scope
- This document is the clean rebuild reference for the Aula Movil Raspberry Pi stack.
- It avoids passwords, tokens, Wi-Fi keys, camera credentials and FTP credentials.
- Secrets belong in runtime files outside git.

## Repositories And Branches
- Main Pi application:
  - branch `main`
- Public PHP receiver and onepage:
  - branch `movilcloud-site`

## Runtime Topology
- `rotor-web`
  - FastAPI + Uvicorn on port `8000`
- `rotor-collector`
  - MQTT, GPS, Sensor.Community and SQLite ingestion
- `rotor-camera`
  - RTSP to HLS bridge
- `rotor-cloud-bridge`
  - optional sender from the Pi to the external `NUBEMOVIL` receiver
- relay ESP
  - dual control for vapor and fan
- LED signs
  - horizontal and vertical variants can consume the same sign endpoint

## Target Layout On The Pi
- Linux user:
  - `aulamovil`
- Repo path:
  - `/opt/rotor-meteo`
- Python venv:
  - `/opt/rotor-meteo/.venv`
- Secrets file:
  - `/etc/rotor-meteo/secrets.yaml`
- Writable runtime directories:
  - `/opt/rotor-meteo/data/hls`
  - `/opt/rotor-meteo/data/sounds`
  - `/opt/rotor-meteo/data/timelapse`
  - `/opt/rotor-meteo/data`

## System Packages
- Python `3.13.x`
- `ffmpeg`
- `mosquitto`
- ALSA utilities:
  - `aplay`
  - `amixer`

## Python Dependencies
- install from `requirements.txt`
- currently pinned:
  - `fastapi==0.115.0`
  - `uvicorn==0.30.6`
  - `paho-mqtt==2.1.0`
  - `PyYAML==6.0.2`
  - `pyserial==3.5`
  - `pynmea2==1.19.0`

## Core Config In Git
- app config:
  - `/opt/rotor-meteo/config/app.yaml`
- metric registry:
  - `/opt/rotor-meteo/config/registry.yaml`

## Secrets Outside Git
- `/etc/rotor-meteo/secrets.yaml`
- expected to contain only deployment-specific secrets such as:
  - camera credentials
  - `cloud_bridge.token`
  - relay control URLs and tokens
  - any local overrides that must not be committed

## Current Network Model
- `wlan0` joins the local Wi-Fi used by the station
- current live deployment uses:
  - SSID `NUBEMOVIL`
- current validated Pi IP on that LAN:
  - `192.168.1.109`
- `eth0` is reserved for the camera link
- current camera-side static Pi address:
  - `192.168.50.2/24`
- current camera fixed IP:
  - `192.168.50.10`
- current Sensor.Community URL in the working deployment:
  - `http://192.168.1.110/data.json`

## Current Reserved Device Map
- `192.168.1.109`
  - Pi `AULAMOVIL`
- `192.168.1.110`
  - Sensor.Community / PM
- `192.168.1.108`
  - vapor + fan relay ESP
- `192.168.1.100`
  - wind + direction + ground BME ESP
- `192.168.1.101`
  - light / UV ESP
- `192.168.1.102`
  - vertical LED sign
- `192.168.1.103`
  - rain / pluviometer ESP
- `192.168.1.105`
  - horizontal LED sign

## Service Units
- install these in `/etc/systemd/system/`:
  - `rotor-web.service`
  - `rotor-collector.service`
  - `rotor-camera.service`
  - `rotor-cloud-bridge.service`
- then run:
  - `systemctl daemon-reload`
  - `systemctl enable --now rotor-web rotor-collector rotor-camera`
- enable `rotor-cloud-bridge` only when the external receiver is configured

## Minimal Build Order
1. Create user `aulamovil`.
2. Clone `main` into `/opt/rotor-meteo`.
3. Create the venv in `/opt/rotor-meteo/.venv`.
4. Install `requirements.txt`.
5. Install `ffmpeg`, `mosquitto` and ALSA utilities.
6. Create `/etc/rotor-meteo/secrets.yaml`.
7. Create `data/hls`, `data/sounds`, `data/timelapse` if missing.
8. Install the systemd units.
9. Enable `rotor-web`, `rotor-collector` and `rotor-camera`.
10. Run `./scripts/install.sh` to:
  - create the venv
  - install Python requirements
  - install systemd units
  - bootstrap versioned runtime assets into `data/` if missing
11. Validate local web, HLS and database writes before enabling `rotor-cloud-bridge`.

## Audio Notes
- Analog jack output:
  - `plughw:CARD=Headphones,DEV=0`
- Current USB speaker seen in the working deployment:
  - `plughw:CARD=CD002,DEV=0`
- Runtime sound config:
  - `/opt/rotor-meteo/data/sound_config.json`
- Vapor sequence state:
  - `/opt/rotor-meteo/data/vapor_sequence.json`
- Vapor automation rules:
  - `/opt/rotor-meteo/data/vapor_automation.json`
- If USB audio is unreliable, the jack output is the safest fallback.
- `scripts/install.sh` copies these versioned runtime assets into live `data/` only when they do not already exist.
- It intentionally does not overwrite:
  - `data/rotor.db*`
  - `data/meteo.db`
  - `data/hls/*`

## Power Notes
- If the Pi behaves erratically, check:
  - `vcgencmd get_throttled`
  - `lsusb`
  - `aplay -l`
- USB audio issues were previously correlated with undervoltage.

## NUBEMOVIL Bridge
- Purpose:
  - keep the Pi stack unchanged while forwarding a side-channel of data and frames
- Current production endpoint:
  - `https://www.rotor-studio.net/nubemovil/cloud_ingest.php`
- Reads from the Pi:
  - `http://127.0.0.1:8000/api/latest`
  - `http://127.0.0.1:8000/api/pi/health` for `TEMP PI`
  - `/opt/rotor-meteo/data/hls/latest.jpg`
- Runtime-only secret:
  - `cloud_bridge.token`
- Public read endpoints:
  - `https://www.rotor-studio.net/nubemovil/cloud_state.php`
  - `https://www.rotor-studio.net/nubemovil/cloud_frame.php`
- Public branded entry currently used:
  - `https://www.rotor-studio.net/nubemovil/nubemovil.php`

## Local 24h Interpretation
- Phrase selection depends on the current logical state and a state-derived seed.
- If a state stays stable, phrase variants may rotate every 180 seconds.
- If a relevant sensor change alters the state, the phrase can change immediately.

## Relay Control
- Web control lives inside `rotor-web`
- Local endpoints:
  - `GET /api/vapor/state`
  - `POST /api/vapor/set`
  - `GET /api/fan/state`
  - `POST /api/fan/set`
  - `GET /api/vapor/sequence`
  - `POST /api/vapor/sequence/record`
  - `POST /api/vapor/sequence/play`
  - `POST /api/vapor/sequence/stop`
  - `POST /api/vapor/sequence/clear`
  - `GET /api/vapor/automation`
  - `POST /api/vapor/automation/rules`
  - `DELETE /api/vapor/automation/rules/{id}`
- The relay ESP is a separate module on Wi-Fi and should be configured only through:
  - `/etc/rotor-meteo/secrets.yaml`
- Current logical pin map in the repo:
  - `vapor -> D6`
  - `fan -> D2`
- Do not hardcode the live relay IP in git-driven config for a rebuild recipe.
- Current operator workflow:
  - record a relay sequence from the dashboard
  - replay it manually from the same tab
  - optionally bind sensor thresholds to replay that sequence automatically

## LED Signs
- Shared endpoint for sign clients:
  - `http://192.168.1.109:8000/api/sign/latest`
- Horizontal sketch in current repo:
  - `/opt/rotor-meteo/LEDPANEL/cartel_nubemovil_64x8`
- Vertical sketch in current repo:
  - `/opt/rotor-meteo/esp8266/cartel_pronostico_vertical_rain`
- Use the same payload logic for multiple panels when the hardware is equivalent.
- Horizontal sign notes:
  - custom `5x6` compact font
  - `1 px` top margin and `1 px` bottom margin
  - startup shows `BOOT`
  - waits `60000 ms` before first Wi-Fi connect attempt
- Vertical sign notes:
  - text-only when `display.effect = none`
  - effect-only when `display.effect` is `rain`, `dust` or `lightning`
  - startup shows `BOOT`
  - waits `60000 ms` before first Wi-Fi connect attempt

## FX Controls
- `rotor-web` now includes an `FX` tab
- available endpoints:
  - `GET /api/fx/state`
  - `POST /api/fx/text-color`
  - `POST /api/fx/effect`
- `api/sign/latest` can inject:
  - `display.color`
  - `display.effect`
- current effect values:
  - `none`
  - `rain`
  - `dust`
  - `lightning`
- current text color modes:
  - `auto`
  - `white`
  - `green`
  - `yellow`
  - `orange`
  - `red`
  - `blue`
  - `purple`
- keep Wi-Fi secrets and relay tokens out of git; committed sketches should use placeholders

## Validation Checklist
- `systemctl is-active rotor-web rotor-collector rotor-camera`
- `curl http://127.0.0.1:8000/api/sound/state`
 - `curl http://127.0.0.1:8000/api/vapor/sequence`
 - `curl http://127.0.0.1:8000/api/vapor/automation`
- `lsusb`
- `aplay -l`
- `vcgencmd get_throttled`
- open `http://<pi-ip>:8000/`
- verify `/hls/stream.m3u8`
- verify `/api/vapor/state` and `/api/fan/state` when the relay module is enabled
- confirm `/opt/rotor-meteo/data/rotor.db` is growing
- confirm `sensor_community_1` values appear in `/api/latest`
- confirm `bme280_ground` values appear in `/api/latest`
- if using the cloud bridge:
  - `systemctl is-active rotor-cloud-bridge`
  - `journalctl -u rotor-cloud-bridge -f`

