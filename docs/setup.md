# Rotor Meteo Setup Notes

## MQTT Topic Convention
Prefix: `rotor/meteo`

Topic: `rotor/meteo/<device_id>/<metric_id>`

Payload (JSON):
```
{
  "value": 23.4,
  "unit": "C",
  "ts": "2026-03-03T12:00:00Z"
}
```
- `value` is required
- `unit` optional
- `ts` optional (ISO8601 or epoch seconds)

## Services
- `rotor-collector.service` subscribes to MQTT, normalizes and writes to SQLite.
- `rotor-web.service` serves API + dashboard.

## API
- `GET /api/latest`
- `GET /api/history?metric=...&from=...&to=...`
- `GET /api/stream` (SSE)

## Access
Dashboard: `http://<pi-ip>:8000/`

## Install
```
cd /opt/rotor-meteo
./scripts/install.sh
```
