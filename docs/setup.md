# Rotor Meteo Setup Notes

## Network Mode (Current)
We are **not** using AP mode. The Pi connects to the existing Wi-Fi network:
- Wi-Fi: ROTORLINK on wlan0
- eth0 reserved for CCTV camera (kept disconnected by default)

If you ever need to clean AP mode, remove hostapd and dnsmasq and ensure only wlan0 is connected via NetworkManager.

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

## API
- GET /api/latest
- GET /api/history?metric=...&from=...&to=...
- GET /api/stream (SSE)

## Access
Dashboard: http://<pi-ip>:8000/

## Install
`
cd /opt/rotor-meteo
./scripts/install.sh
`
