#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/rotor-meteo
VENV=/.venv

python3 -m venv  
/bin/pip install --upgrade pip
/bin/pip install -r /requirements.txt

sudo mkdir -p /opt/rotor-meteo/data /opt/rotor-meteo/data/hls
sudo chown -R aulamovil:aulamovil /opt/rotor-meteo/data

sudo cp /scripts/systemd/rotor-collector.service /etc/systemd/system/rotor-collector.service
sudo cp /scripts/systemd/rotor-web.service /etc/systemd/system/rotor-web.service
sudo cp /scripts/systemd/rotor-camera.service /etc/systemd/system/rotor-camera.service
sudo cp /scripts/systemd/rotor-camera-snapshot.service /etc/systemd/system/rotor-camera-snapshot.service
sudo systemctl daemon-reload
sudo systemctl enable --now rotor-collector.service
sudo systemctl enable --now rotor-web.service
sudo systemctl enable --now rotor-camera.service
sudo systemctl enable --now rotor-camera-snapshot.service

sudo systemctl enable --now mosquitto.service

echo Install complete.
