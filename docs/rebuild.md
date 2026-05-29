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
  - SSID `ROTORLINK`
- `eth0` is reserved for the camera link
- current camera-side static Pi address:
  - `192.168.50.2/24`
- current camera fixed IP:
  - `192.168.50.10`

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
10. Validate local web, HLS and database writes before enabling `rotor-cloud-bridge`.

## Audio Notes
- Analog jack output:
  - `plughw:CARD=Headphones,DEV=0`
- Current USB speaker seen in the working deployment:
  - `plughw:CARD=CD002,DEV=0`
- Runtime sound config:
  - `/opt/rotor-meteo/data/sound_config.json`
- If USB audio is unreliable, the jack output is the safest fallback.

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
  - `/opt/rotor-meteo/data/hls/latest.jpg`
- Runtime-only secret:
  - `cloud_bridge.token`
- Public read endpoints:
  - `https://www.rotor-studio.net/nubemovil/cloud_state.php`
  - `https://www.rotor-studio.net/nubemovil/cloud_frame.php`
- Public branded entry currently used:
  - `https://www.rotor-studio.net/nubemovil/nubemovil.php`

## Relay Control
- Web control lives inside `rotor-web`
- Local endpoints:
  - `GET /api/vapor/state`
  - `POST /api/vapor/set`
  - `GET /api/fan/state`
  - `POST /api/fan/set`
- The relay ESP is a separate module on Wi-Fi and should be configured only through:
  - `/etc/rotor-meteo/secrets.yaml`
- Current logical pin map in the repo:
  - `vapor -> D6`
  - `fan -> D2`
- Do not hardcode the live relay IP in git-driven config for a rebuild recipe.

## LED Signs
- Shared endpoint for sign clients:
  - `http://<pi-ip>:8000/api/sign/latest`
- Horizontal sketch:
  - `/opt/rotor-meteo/esp8266/cartel_pronostico`
- Vertical sketch:
  - `/opt/rotor-meteo/esp8266/cartel_pronostico_vertical`
- Use the same payload logic for multiple panels when the hardware is equivalent.

## Validation Checklist
- `systemctl is-active rotor-web rotor-collector rotor-camera`
- `curl http://127.0.0.1:8000/api/sound/state`
- `lsusb`
- `aplay -l`
- `vcgencmd get_throttled`
- open `http://<pi-ip>:8000/`
- verify `/hls/stream.m3u8`
- verify `/api/vapor/state` and `/api/fan/state` when the relay module is enabled
- confirm `/opt/rotor-meteo/data/rotor.db` is growing
- if using the cloud bridge:
  - `systemctl is-active rotor-cloud-bridge`
  - `journalctl -u rotor-cloud-bridge -f`

