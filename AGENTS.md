# Rotor Meteo AGENTS Notes

## Current Operating Mode
- No AP mode in use.
- wlan0 connects to Wi-Fi network ROTORLINK.
- eth0 reserved for CCTV camera and kept disconnected by default.

## Services
- mosquitto running locally.
- otor-collector and otor-web enabled and running.

## Repo Location
- Project repo: /opt/rotor-meteo

## Access
- Web UI: http://<pi-ip>:8000/
- MQTT topic prefix: otor/meteo

## SSH
- Primary SSH access over wlan0 on the ROTORLINK network.
