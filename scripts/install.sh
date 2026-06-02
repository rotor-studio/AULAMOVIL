#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
DATA_DIR="$ROOT/data"
SYSTEMD_DIR="$ROOT/scripts/systemd"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"

copy_if_missing() {
  local src="$1"
  local dst="$2"
  if [[ -e "$src" && ! -e "$dst" ]]; then
    install -D -m 0644 "$src" "$dst"
  fi
}

sync_directory_contents() {
  local src_dir="$1"
  local dst_dir="$2"
  if [[ ! -d "$src_dir" ]]; then
    return 0
  fi
  mkdir -p "$dst_dir"
  while IFS= read -r -d '' src; do
    local rel="${src#$src_dir/}"
    local dst="$dst_dir/$rel"
    if [[ ! -e "$dst" ]]; then
      install -D -m 0644 "$src" "$dst"
    fi
  done < <(find "$src_dir" -type f -print0)
}

echo "==> Creating virtualenv in $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"

echo "==> Preparing runtime directories"
mkdir -p \
  "$DATA_DIR" \
  "$DATA_DIR/hls" \
  "$DATA_DIR/timelapse" \
  "$DATA_DIR/messages" \
  "$DATA_DIR/sounds"

echo "==> Bootstrapping versioned runtime assets"
copy_if_missing "$ROOT/data/fx_config.json" "$DATA_DIR/fx_config.json"
copy_if_missing "$ROOT/data/rain_window_config.json" "$DATA_DIR/rain_window_config.json"
copy_if_missing "$ROOT/data/sound_config.json" "$DATA_DIR/sound_config.json"
copy_if_missing "$ROOT/data/vapor_automation.json" "$DATA_DIR/vapor_automation.json"
copy_if_missing "$ROOT/data/vapor_sequence.json" "$DATA_DIR/vapor_sequence.json"
copy_if_missing "$ROOT/data/wind_calibration.json" "$DATA_DIR/wind_calibration.json"
sync_directory_contents "$ROOT/data/messages" "$DATA_DIR/messages"
sync_directory_contents "$ROOT/data/sounds" "$DATA_DIR/sounds"

echo "==> Fixing ownership for $RUN_USER:$RUN_GROUP"
chown -R "$RUN_USER:$RUN_GROUP" "$DATA_DIR"

echo "==> Installing systemd units"
for unit in \
  rotor-collector.service \
  rotor-web.service \
  rotor-camera.service \
  rotor-camera-snapshot.service \
  rotor-cloud-bridge.service
do
  if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
    sudo cp "$SYSTEMD_DIR/$unit" "/etc/systemd/system/$unit"
  fi
done

sudo systemctl daemon-reload

echo "==> Enabling core services"
sudo systemctl enable rotor-collector.service rotor-web.service rotor-camera.service rotor-camera-snapshot.service >/dev/null 2>&1 || true
sudo systemctl enable mosquitto.service >/dev/null 2>&1 || true

echo "==> Starting core services"
sudo systemctl restart rotor-collector.service
sudo systemctl restart rotor-web.service
sudo systemctl restart rotor-camera.service
sudo systemctl restart rotor-camera-snapshot.service

echo "Install complete."
