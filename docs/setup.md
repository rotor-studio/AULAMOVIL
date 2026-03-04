# Rotor Meteo Setup Notes

## Network Mode (Current)
- Wi-Fi: ROTORLINK on wlan0
- eth0 reserved for CCTV camera and provides DHCP on 192.168.50.0/24
- Camera network DHCP range: 192.168.50.10 - 192.168.50.100
- Pi on camera network: 192.168.50.1/24

## Camera (RTSP + HLS)
We convert RTSP to HLS locally with ffmpeg.
- HLS playlist: /hls/stream.m3u8
- Web UI uses HLS.js from /static/hls.min.js
- RTSP URL configured in config/app.yaml
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

## Access
Dashboard: http://<pi-ip>:8000/

## Install
`
cd /opt/rotor-meteo
./scripts/install.sh
`
