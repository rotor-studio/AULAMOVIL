# NUBEMOVIL Setup Notes

## Network Mode (Current)
- Wi-Fi `NUBEMOVIL` on `wlan0`
- Current validated Pi IP on that LAN: `192.168.1.109`
- eth0 reserved for the CCTV camera on a direct link with static addressing.
- Pi on camera network: 192.168.50.2/24
- Camera IP: 192.168.50.10/24
- Sensor.Community currently expected at: `http://192.168.1.110/data.json`

## Reserved Devices
- `192.168.1.109` Pi `AULAMOVIL`
- `192.168.1.110` Sensor.Community / PM
- `192.168.1.108` relay ESP for vapor and fan
- `192.168.1.100` wind + direction + ground BME
- `192.168.1.101` light / UV
- `192.168.1.102` vertical LED sign
- `192.168.1.103` rain / pluviometer
- `192.168.1.105` horizontal LED sign

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
- GET /api/fx/state
- POST /api/fx/text-color
- POST /api/fx/effect
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
  - `http://192.168.1.109:8000/api/sign/latest`
- Horizontal sign sketch:
  - `LEDPANEL/cartel_nubemovil_64x8`
- Vertical sign sketch:
  - `esp8266/cartel_pronostico_vertical_rain`
- Horizontal sign details:
  - custom `5x6` font
  - `1 px` free above and `1 px` free below
  - `BOOT` shown for `60000 ms` before first Wi-Fi attempt
- Vertical sign details:
  - text-only when `effect = none`
  - effect-only for `rain`, `dust`, `lightning`
  - `BOOT` shown for `60000 ms` before first Wi-Fi attempt

## FX
- Dashboard tab `FX` can force:
  - text color
  - sign effect
- Current effect modes:
  - `none`
  - `rain`
  - `dust`
  - `lightning`
- Current text color modes:
  - `auto`
  - `white`
  - `green`
  - `yellow`
  - `orange`
  - `red`
  - `blue`
  - `purple`

## Access
Dashboard: http://<pi-ip>:8000/

## Runtime Notes
- Relay ESP IP is DHCP-driven unless reserved in the router.
- Ground BME values arrive through `meteo/env/temperature_c` and `meteo/env/humidity`.
- GPS can remain stale even when `/dev/ttyACM0` is present if the receiver has no valid fix.
- Sketches committed to git should keep placeholder Wi-Fi credentials and tokens.

## Install
`
cd /opt/rotor-meteo
./scripts/install.sh
`
