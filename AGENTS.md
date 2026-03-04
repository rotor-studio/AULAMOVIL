# Rotor Meteo AGENTS Notes

## Current Operating Mode
- No AP mode in use.
- wlan0 connects to Wi-Fi network ROTORLINK.
- eth0 reserved for CCTV camera and provides DHCP on 192.168.50.0/24.
- Pi camera-network IP: 192.168.50.1.

## Camera
- RTSP config in config/app.yaml.
- Passwords stored in /etc/rotor-meteo/secrets.yaml (not in git).
- HLS served at /hls/stream.m3u8.
- HLS.js served at /static/hls.min.js.

## Services
- mosquitto running locally.
- otor-collector, otor-web, and otor-camera enabled and running.
- isc-dhcp-server provides camera DHCP on eth0.

## Repo Location
- Project repo: /opt/rotor-meteo

## Access
- Web UI: http://<pi-ip>:8000/
- MQTT topic prefix: otor/meteo

## SSH
- Primary SSH access over wlan0 on the ROTORLINK network.
