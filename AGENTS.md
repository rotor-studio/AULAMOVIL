# Rotor Meteo AGENTS Notes

## Purpose
- This repo runs a local weather station stack on a Raspberry Pi.
- Main repo path on the Pi: `/opt/rotor-meteo`
- Main host in the current deployment: `AULAMOVIL`

## Runtime Topology
- `rotor-web`: FastAPI + Uvicorn on port `8000`
- `rotor-collector`: MQTT, GPS, Sensor.Community and SQLite ingestion
- `rotor-camera`: RTSP to HLS bridge
- Local SQLite DB:
  - `/opt/rotor-meteo/data/rotor.db`
- Web-served HLS output:
  - `/opt/rotor-meteo/data/hls`
- Uploaded sounds:
  - `/opt/rotor-meteo/data/sounds`
- Timelapse output:
  - `/opt/rotor-meteo/data/timelapse`

## Required Layout On A New Pi
- Linux user:
  - `aulamovil`
- Repo checkout:
  - `/opt/rotor-meteo`
- Python venv:
  - `/opt/rotor-meteo/.venv`
- Secrets file:
  - `/etc/rotor-meteo/secrets.yaml`
- Systemd units installed in:
  - `/etc/systemd/system/rotor-web.service`
  - `/etc/systemd/system/rotor-collector.service`
  - `/etc/systemd/system/rotor-camera.service`

## Python Requirements
- Installed from `requirements.txt`
- Current pinned dependencies:
  - `fastapi==0.115.0`
  - `uvicorn==0.30.6`
  - `paho-mqtt==2.1.0`
  - `PyYAML==6.0.2`
  - `pyserial==3.5`
  - `pynmea2==1.19.0`

## OS-Level Requirements
- Python `3.13.x`
- `ffmpeg`
- ALSA utils:
  - `aplay`
  - `amixer`
- Local MQTT broker expected by config:
  - `mosquitto`

## Config Files
- Main app config in git:
  - `/opt/rotor-meteo/config/app.yaml`
- Metric registry in git:
  - `/opt/rotor-meteo/config/registry.yaml`
- Secrets outside git:
  - `/etc/rotor-meteo/secrets.yaml`

## Current app.yaml Assumptions
- MQTT:
  - host `127.0.0.1`
  - port `1883`
  - topic prefix `rotor/meteo`
- Web:
  - host `0.0.0.0`
  - port `8000`
- Camera:
  - host `192.168.50.10`
  - RTSP path `/Streaming/Channels/101`
- GPS:
  - device `/dev/ttyACM0`
  - baud `9600`
- Sensor.Community:
  - URL `http://192.168.0.171/data.json`

## Current Network Model
- No AP mode in use
- `wlan0` joins Wi-Fi `ROTORLINK`
- `eth0` is reserved for the CCTV camera network
- Current camera-side static Pi address:
  - `192.168.50.2/24`
- Camera fixed IP:
  - `192.168.50.10`

## Services
- `rotor-web`
  - working dir: `/opt/rotor-meteo`
  - exec: `/opt/rotor-meteo/.venv/bin/uvicorn web.main:app --host 0.0.0.0 --port 8000`
- `rotor-collector`
  - working dir: `/opt/rotor-meteo`
  - exec: `/opt/rotor-meteo/.venv/bin/python /opt/rotor-meteo/collector/collector.py`
- `rotor-camera`
  - working dir: `/opt/rotor-meteo`
  - exec: `/opt/rotor-meteo/.venv/bin/python /opt/rotor-meteo/scripts/camera_hls.py`

## Enable On A New Pi
1. Create user `aulamovil` if not present.
2. Clone repo into `/opt/rotor-meteo`.
3. Create venv in `/opt/rotor-meteo/.venv`.
4. Install `requirements.txt` into that venv.
5. Install OS packages:
   - `ffmpeg`
   - `mosquitto`
   - ALSA utilities
6. Create `/etc/rotor-meteo/secrets.yaml`.
7. Copy or install the systemd service files.
8. Create data directories if missing:
   - `data/hls`
   - `data/sounds`
   - `data/timelapse`
9. `systemctl daemon-reload`
10. `systemctl enable --now rotor-web rotor-collector rotor-camera`

## Audio
- Current USB speaker detected by ALSA as:
  - `plughw:CARD=CD002,DEV=0`
- Current analog jack output:
  - `plughw:CARD=Headphones,DEV=0`
- Sound config persisted in:
  - `/opt/rotor-meteo/data/sound_config.json`
- `sound_config.json` now supports:
  - `enabled`
  - `rules`
  - `output_device`
- Current UI behavior:
  - manual sound tests
  - file upload and delete
  - sensor-driven rules
  - rule activity indicators
  - output selection buttons for `USB` and `Jack`
- Current sound API endpoints:
  - `GET /api/sound/state`
  - `POST /api/sound/global`
  - `POST /api/sound/output`
  - `POST /api/sound/stop`
  - `POST /api/sound/test/chirp`
  - `POST /api/sound/test/file`
  - `POST /api/sound/upload`
  - `DELETE /api/sound/files/{filename}`
  - `POST /api/sound/rules`
  - `DELETE /api/sound/rules/{id}`

## Current Audio Notes
- Audio output is selected by `config["output_device"]` from `data/sound_config.json`.
- The engine falls back through available devices if the configured one is unavailable.
- The audio loop was changed so it does not hold the main lock while blocking on `aplay` pipe writes.
- This avoids web freezes when the audio backend stalls.

## Camera / HLS
- HLS playlist served at:
  - `/hls/stream.m3u8`
- Frontend ships:
  - `/static/hls.min.js`
- Camera credentials are not committed and must come from `/etc/rotor-meteo/secrets.yaml`.

## ESP8266 Sign
- Sketch repo path:
  - `/opt/rotor-meteo/esp8266/cartel_pronostico`
- Current matrix assumptions:
  - `38x8`
  - zigzag wiring
- Current compact endpoint for sign/panel:
  - `/api/sign/latest`

## Validation Checklist After Deploy
- `systemctl is-active rotor-web rotor-collector rotor-camera`
- `curl http://127.0.0.1:8000/api/sound/state`
- `aplay -l`
- `lsusb`
- `vcgencmd get_throttled`
- open `http://<pi-ip>:8000/`
- verify HLS stream
- verify SQLite file creation in `data/`
- verify GPS device exists if enabled
- verify MQTT messages enter SQLite

## Known Operational Risks
- USB audio can appear and disappear depending on power quality.
- Undervoltage must be treated seriously on Raspberry Pi:
  - check `vcgencmd get_throttled`
- If USB audio is unreliable, jack output is a valid fallback.
- Heavy browser polling or open HLS clients can delay a graceful `rotor-web` restart.

## Restart Pattern That Has Worked Best
- If `rotor-web` does not come back cleanly:
  - stop or kill `uvicorn`
  - `systemctl reset-failed rotor-web`
  - `systemctl start rotor-web`

## SSH / Access
- Main SSH access is over `wlan0`
- Web UI:
  - `http://<pi-ip>:8000/`
- Compact sign endpoint:
  - `http://<pi-ip>:8000/api/sign/latest`
