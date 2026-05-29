# NUBEMOVIL Setup Notes

## Network Mode (Current)
- Wi-Fi `NUBEMOVIL` on `wlan0`
- eth0 reserved for the CCTV camera on a direct link with static addressing.
- Pi on camera network: 192.168.50.2/24
- Camera IP: 192.168.50.10/24

## Camera (RTSP + HLS)
We convert RTSP to HLS locally with ffmpeg.
- HLS playlist: /hls/stream.m3u8
- Web UI uses HLS.js from /static/hls.min.js
- RTSP URL configured in config/app.yaml
- Hikvision camera management port: 8000
- **Passwords are stored in /etc/rotor-meteo/secrets.yaml (not in git)**

Example RTSP:
`
rtsp://<user>:<pass>@<camera-ip>:554/Streaming/Channels/101
`

## MQTT Topic Convention
Prefix: otor/meteo

Topic: otor/meteo/<device_id>/<metric_id>

Payload (JSON):
`
{
   value: 23.4,
  unit: C,
  ts: 2026-03-03T12:00:00Z
}
`
- alue is required
- unit optional
- 	s optional (ISO8601 or epoch seconds)

## Services
- otor-collector.service subscribes to MQTT, normalizes and writes to SQLite.
- otor-web.service serves API + dashboard.
- otor-camera.service runs ffmpeg to create HLS.

## API
- GET /api/latest
- GET /api/history?metric=...&from=...&to=...
- GET /api/stream (SSE)
- GET /api/camera
- GET /api/sign/latest
- GET /api/vapor/state
- POST /api/vapor/set
- GET /api/fan/state
- POST /api/fan/set

## Relay ESP
- Dual relay module:
  - vapor on `D6`
  - fan on `D2`
- The ESP address is expected to be provided at runtime in:
  - `/etc/rotor-meteo/secrets.yaml`
- If DHCP changes the relay IP, update:
  - `vapor.base_url`
  - `fan.base_url`

## LED Signs
- Main sign endpoint:
  - `http://<pi-ip>:8000/api/sign/latest`
- Horizontal sign sketch:
  - `esp8266/cartel_pronostico`
- Vertical sign sketch:
  - `esp8266/cartel_pronostico_vertical`

## Access
Dashboard: http://<pi-ip>:8000/

## Install
`
cd /opt/rotor-meteo
./scripts/install.sh
`
