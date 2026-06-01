# NUBEMOVIL AGENTS Notes

## Purpose
- This repo runs a local weather station stack on a Raspberry Pi.
- Main repo path on the Pi: `/opt/rotor-meteo`
- Main host in the current deployment: `AULAMOVIL`
- Public branding in the current deployment: `NUBEMOVIL`
- Rebuild reference:
  - `docs/rebuild.md`

## Runtime Topology
- `rotor-web`: FastAPI + Uvicorn on port `8000`
- `rotor-collector`: MQTT, GPS, Sensor.Community and SQLite ingestion
- `rotor-camera`: RTSP to HLS bridge
- `rotor-cloud-bridge`: optional sender from the Pi to the external NUBEMOVIL receiver
- Relay controller:
  - ESP8266 dual relay for `vapor` and `fan`
- LED sign clients:
  - horizontal sign
  - vertical sign
- Local SQLite DB:
  - `/opt/rotor-meteo/data/rotor.db`
- Web-served HLS output:
  - `/opt/rotor-meteo/data/hls`
- Uploaded sounds:
  - `/opt/rotor-meteo/data/sounds`
- Timelapse output:
  - `/opt/rotor-meteo/data/timelapse`

## MOVILCLOUD App In Repo
- Branch `movilcloud-site` also contains a PHP receiver app in `/movilcloud`
- Current validated state:
  - the bridge publishes `TEMP PI` from `pi_health/temperature_c`
  - `display` stays for cartels while `web_display` is used by the public web entry
- Main files:
  - `/movilcloud/public/index.php`
  - `/movilcloud/public/api/ingest.php`
  - `/movilcloud/public/api/state.php`
  - `/movilcloud/public/api/frame.php`
- The committed app is template-safe:
  - runtime `storage/` contents are not committed
  - `config/config.php` keeps a placeholder token and must be edited per deployment
  - current UI includes responsive mobile scroll behavior

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
  - `/etc/systemd/system/rotor-cloud-bridge.service`

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

## Runtime Overrides In secrets.yaml
- `camera.password`
- `cloud_bridge.enabled`
- `cloud_bridge.token`
- `vapor.enabled`
- `vapor.base_url`
- `vapor.token`
- `fan.enabled`
- `fan.base_url`
- `fan.token`

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
  - URL `http://192.168.1.110/data.json`

## Current Network Model
- No AP mode in use
- `wlan0` joins Wi-Fi `NUBEMOVIL`
- current validated Pi IP on that LAN:
  - `192.168.1.109`
- `eth0` is reserved for the CCTV camera network
- Current camera-side static Pi address:
  - `192.168.50.2/24`
- Camera fixed IP:
  - `192.168.50.10`

## Current Reserved LAN Map
- `192.168.1.109`
  - Pi `AULAMOVIL`
- `192.168.1.110`
  - Sensor.Community / PM node
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

## Relay Control
- Local web endpoints:
  - `GET /api/vapor/state`
  - `POST /api/vapor/set`
  - `GET /api/fan/state`
  - `POST /api/fan/set`
- Relay module behavior:
  - same ESP8266 serves both `vapor` and `fan`
  - current logical pin map in the repo:
    - `vapor -> D6`
    - `fan -> D2`
- Runtime note:
  - the relay ESP may change DHCP address unless the router reserves it
  - when that happens, update `vapor.base_url` and `fan.base_url` in `/etc/rotor-meteo/secrets.yaml`
- do not hardcode the live relay IP in git notes; it is DHCP unless reserved in the router

## Current Sensor State
- Working on `NUBEMOVIL` at last validation:
  - rain node
  - wind/direction node
  - ground BME node via `meteo/env/*`
  - light/UV node via `light_mcu`
  - Sensor.Community at `192.168.1.110`
- GPS device presence:
  - serial device still appears on `/dev/ttyACM0`
  - last known position may remain stale if the receiver has no valid fix

## FX Tab
- `rotor-web` now exposes an `FX` tab in the dashboard
- Current manual controls in `FX`:
  - text color mode:
    - `auto`
    - `white`
    - `green`
    - `yellow`
    - `orange`
    - `red`
    - `blue`
    - `purple`
  - display effect mode:
    - `none`
    - `rain`
    - `dust`
    - `lightning`
- API endpoints:
  - `GET /api/fx/state`
  - `POST /api/fx/text-color`
  - `POST /api/fx/effect`
- `compute_forecast_payload()` injects into `display`:
  - optional `color`
  - optional `effect`

## LED Sign Firmware State
- Horizontal production-style sign:
  - `LEDPANEL/cartel_nubemovil_64x8/cartel_nubemovil_64x8.ino`
  - endpoint:
    - `http://192.168.1.109:8000/api/sign/latest`
  - custom horizontal compact font:
    - `5x6`
    - `1 px` empty top margin
    - `1 px` empty bottom margin
    - `6 px` advance per character
  - startup behavior:
    - shows `BOOT`
    - waits `60000 ms` before first Wi-Fi attempt
- Vertical production-style sign:
  - `esp8266/cartel_pronostico_vertical_rain/cartel_pronostico_vertical_rain.ino`
  - endpoint:
    - `http://192.168.1.109:8000/api/sign/latest`
  - text-only by default when `display.effect = none`
  - effect-only modes when `display.effect` is:
    - `rain`
    - `dust`
    - `lightning`
  - startup behavior:
    - shows `BOOT`
    - waits `60000 ms` before first Wi-Fi attempt
- Repo safety:
  - committed sketches must keep placeholder Wi-Fi credentials and tokens
  - real SSID, password and relay tokens stay outside git

## Local 24h Interpretation
- Phrase selection depends on the current logical state and a state-derived seed.
- If a state stays stable, phrase variants may rotate every 180 seconds.
- If a relevant sensor change alters the state, the phrase can change immediately.

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
- `rotor-cloud-bridge`
  - working dir: `/opt/rotor-meteo`
  - exec: `/opt/rotor-meteo/.venv/bin/python /opt/rotor-meteo/scripts/cloud_bridge.py`
  - reads local `http://127.0.0.1:8000/api/latest`
  - reads local `http://127.0.0.1:8000/api/pi/health` for `TEMP PI`
  - reads latest camera frame from `/opt/rotor-meteo/data/hls/latest.jpg`
  - posts to the external NUBEMOVIL receiver

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

## MOVILCLOUD Bridge
- Purpose:
  - keeps Aula Movil behaving as it already does
  - adds a side-channel sender to an external PHP receiver
- Current production receiver target:
  - `https://www.rotor-studio.net/nubemovil/cloud_ingest.php`
- Current validated `main` commit:
  - `27adb57`
- Legacy local development reachability model:
  - the Windows PC can run `MOVILCLOUD` on `127.0.0.1:8080`
  - a reverse SSH tunnel can expose that receiver inside the Pi as `127.0.0.1:18080`
  - this is no longer the active production path
- Receiver token expected by the local MOVILCLOUD instance:
  - set this only in runtime config; do not commit the real token
- Bridge defaults live in:
  - `/opt/rotor-meteo/config/app.yaml`
- Secrets or overrides should live in:
  - `/etc/rotor-meteo/secrets.yaml`
- Current bridge config keys:
  - `cloud_bridge.enabled`
  - `cloud_bridge.endpoint`
  - `cloud_bridge.token`
  - `cloud_bridge.interval_sec`
  - `cloud_bridge.timeout_sec`
  - `cloud_bridge.title`
  - `cloud_bridge.subtitle`
  - `cloud_bridge.frame_path`
  - `cloud_bridge.include_frame`
  - `cloud_bridge.metrics`
- Current metric set sent to the external NUBEMOVIL receiver:
  - `wind_esp8266/wind_direction_deg -> Direccion`
  - `wind_esp8266/wind_speed_ms -> Velocidad`
  - `light_mcu/light_lux -> Luminosidad`
  - `light_mcu/uv_voltage_v -> Radiacion UV`
  - `rain_node_mcu/rain_mm_total -> Lluvia`
  - `sensor_community_1/temp_c -> Temp`
  - `sensor_community_1/rh_pct -> Humedad`
  - `bme280_ground/temp_ground_c -> Temp suelo`
  - `bme280_ground/rh_ground_pct -> Hum suelo`
  - `sensor_community_1/pm2_5_ugm3 -> PM2.5`
  - `sensor_community_1/pm10_ugm3 -> PM10`
  - `pi_health/temperature_c -> TEMP PI`
- Bridge behavior:
  - sends JSON only
  - includes `frame_base64` when the latest frame file has changed
  - does not modify `rotor-web`, `rotor-camera` or `rotor-collector`
  - failure to reach MOVILCLOUD must not affect the core local stack
- Service install:
  - copy `scripts/systemd/rotor-cloud-bridge.service` to `/etc/systemd/system/`
  - `systemctl daemon-reload`
  - `systemctl enable --now rotor-cloud-bridge`
- Validation:
  - `systemctl status rotor-cloud-bridge`
  - `journalctl -u rotor-cloud-bridge -f`
  - verify `https://www.rotor-studio.net/nubemovil/cloud_state.php`
  - verify `https://www.rotor-studio.net/nubemovil/nubemovil.php`

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
- Vertical variant:
  - `/opt/rotor-meteo/esp8266/cartel_pronostico_vertical`
- Current matrix assumptions:
  - `38x8`
  - zigzag wiring
- Current compact endpoint for sign/panel:
  - `/api/sign/latest`
- Current reuse model:
  - multiple LED signs can consume the same `/api/sign/latest` endpoint
  - use the same logic if the panel hardware is equivalent
  - use the vertical sketch only when text must be rendered letter-by-letter in a column

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
- Practical note:
  - `systemctl restart rotor-web` can linger in `deactivating` while active browsers keep HLS/SSE connections open
  - in that case, `systemctl kill rotor-web` followed by `systemctl start rotor-web` has been more reliable

## SSH / Access
- Main SSH access is over `wlan0`
- Web UI:
  - `http://<pi-ip>:8000/`
- Compact sign endpoint:
  - `http://<pi-ip>:8000/api/sign/latest`
- Relay web root, when the relay ESP is reachable:
  - `http://<relay-ip>/`
