#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/rotor-meteo
VENV=$ROOT/.venv

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"

sudo mkdir -p /opt/rotor-meteo/data
sudo chown -R aulamovil:aulamovil /opt/rotor-meteo/data

sudo cp "$ROOT/scripts/systemd/rotor-collector.service" /etc/systemd/system/rotor-collector.service
sudo cp "$ROOT/scripts/systemd/rotor-web.service" /etc/systemd/system/rotor-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now rotor-collector.service
sudo systemctl enable --now rotor-web.service

sudo systemctl enable --now mosquitto.service

echo "Install complete."
