import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml


LATEST_URL = "http://127.0.0.1:8000/api/latest"
SIGN_URL = "http://127.0.0.1:8000/api/sign/latest"
DEFAULT_FRAME_PATH = "/opt/rotor-meteo/data/hls/latest.jpg"
DEFAULT_INTERVAL_SEC = 12
DEFAULT_TIMEOUT_SEC = 8
DEFAULT_TITLE = "MOVIL CLOUD"
DEFAULT_SUBTITLE = "Aula Movil en directo"
DEFAULT_METRICS = [
    {"source": "wind_esp8266/wind_direction_deg", "label": "Direccion", "decimals": 0, "unit": "deg"},
    {"source": "wind_esp8266/wind_speed_ms", "label": "Velocidad", "decimals": 1, "unit": "m/s"},
    {"source": "light_mcu/light_lux", "label": "Luminosidad", "decimals": 0, "unit": "lux"},
    {"source": "light_mcu/uv_voltage_v", "label": "Radiacion UV", "decimals": 2, "unit": "V"},
    {"source": "rain_node_mcu/rain_mm_total", "label": "Lluvia", "decimals": 1, "unit": "mm"},
    {"source": "sensor_community_1/temp_c", "label": "Temp", "decimals": 1, "unit": "C"},
    {"source": "sensor_community_1/rh_pct", "label": "Humedad", "decimals": 0, "unit": "%"},
    {"source": "bme280_ground/temp_ground_c", "label": "Temp suelo", "decimals": 1, "unit": "C"},
    {"source": "bme280_ground/rh_ground_pct", "label": "Hum suelo", "decimals": 0, "unit": "%"},
    {"source": "sensor_community_1/pm2_5_ugm3", "label": "PM2.5", "decimals": 1, "unit": "ug/m3"},
    {"source": "sensor_community_1/pm10_ugm3", "label": "PM10", "decimals": 1, "unit": "ug/m3"},
    {"label": "Voltaje", "static_value": 12.4, "decimals": 1, "unit": "V"},
]


def load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def merge_dict(left, right):
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config():
    base = load_yaml("/opt/rotor-meteo/config/app.yaml")
    secrets = load_yaml("/etc/rotor-meteo/secrets.yaml")
    return merge_dict(base, secrets)


def fetch_json(url, timeout_sec):
    with urllib.request.urlopen(url, timeout=timeout_sec) as response:
        return json.load(response)


def fetch_latest_map(timeout_sec):
    return fetch_json(LATEST_URL, timeout_sec)


def fetch_sign_state(timeout_sec):
    return fetch_json(SIGN_URL, timeout_sec)


def coerce_number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def format_metric_value(raw_value, decimals):
    numeric = coerce_number(raw_value)
    if numeric is None:
        return str(raw_value)
    if decimals <= 0:
        return str(int(round(numeric)))
    return f"{numeric:.{decimals}f}"


def build_metrics(latest_map, specs):
    metrics = []
    for spec in specs:
        source = str(spec.get("source", "")).strip()
        if "static_value" in spec:
            metrics.append(
                {
                    "label": str(spec.get("label") or "Dato"),
                    "value": format_metric_value(spec.get("static_value"), int(spec.get("decimals", 1))),
                    "unit": str(spec.get("unit") or ""),
                    "device": str(spec.get("device") or "aulamovil"),
                    "metric_id": str(spec.get("metric_id") or "simulated"),
                }
            )
            continue
        if not source:
            continue
        row = latest_map.get(source)
        if not isinstance(row, dict):
            continue
        metrics.append(
            {
                "label": str(spec.get("label") or source),
                "value": format_metric_value(row.get("value"), int(spec.get("decimals", 1))),
                "unit": str(spec.get("unit") or row.get("unit") or ""),
                "device": str(row.get("device_id") or ""),
                "metric_id": str(row.get("metric_id") or ""),
            }
        )
    return metrics


def build_location(latest_map):
    lat_row = latest_map.get("gps_usb_1/gps_lat")
    lon_row = latest_map.get("gps_usb_1/gps_lon")
    lat = coerce_number(lat_row.get("value")) if isinstance(lat_row, dict) else None
    lon = coerce_number(lon_row.get("value")) if isinstance(lon_row, dict) else None
    if lat is None or lon is None:
        return ""
    return f"{lat:.5f}, {lon:.5f}"


def read_frame_base64(frame_path):
    if not os.path.isfile(frame_path):
        return None, None
    stat = os.stat(frame_path)
    with open(frame_path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return encoded, stat.st_mtime


def build_display(sign_state):
    display = sign_state.get("display") if isinstance(sign_state, dict) else {}
    if not isinstance(display, dict):
        display = {}
    return {
        "headline": str(display.get("headline") or ""),
        "line1": str(display.get("line1") or ""),
        "line2": str(display.get("line2") or ""),
    }


def build_web_display(sign_state):
    interpretation = sign_state.get("interpretation") if isinstance(sign_state, dict) else {}
    if not isinstance(interpretation, dict):
        interpretation = {}
    message = interpretation.get("message")
    state = interpretation.get("state")
    if not isinstance(message, dict):
        message = {}
    if not isinstance(state, dict):
        state = {}
    phrase = str(message.get("phrase") or "").strip()
    summary = str(state.get("summary") or "").strip()
    if phrase:
        return {
            "headline": phrase,
            "line1": "",
            "line2": "",
        }
    return build_display(sign_state)


def build_payload(config, latest_map, sign_state, last_frame_mtime):
    bridge = config.get("cloud_bridge", {})
    specs = bridge.get("metrics") or DEFAULT_METRICS
    frame_path = str(bridge.get("frame_path") or DEFAULT_FRAME_PATH)
    include_frame = bool(bridge.get("include_frame", True))
    frame_base64 = None
    frame_mtime = last_frame_mtime

    if include_frame:
        frame_base64, current_mtime = read_frame_base64(frame_path)
        if current_mtime == last_frame_mtime:
            frame_base64 = None
        frame_mtime = current_mtime

    return {
        "token": str(bridge.get("token") or ""),
        "title": str(bridge.get("title") or DEFAULT_TITLE),
        "subtitle": str(bridge.get("subtitle") or DEFAULT_SUBTITLE),
        "location": build_location(latest_map),
        "metrics": build_metrics(latest_map, specs),
        "display": build_display(sign_state),
        "web_display": build_web_display(sign_state),
        "frame_base64": frame_base64,
        "source_ts": datetime.now(timezone.utc).isoformat(),
    }, frame_mtime


def post_payload(endpoint, timeout_sec, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        return response.read().decode("utf-8", errors="replace")


def validate_bridge_config(bridge):
    if not bridge.get("enabled", False):
        raise RuntimeError("cloud_bridge.enabled is false")
    endpoint = str(bridge.get("endpoint") or "").strip()
    token = str(bridge.get("token") or "").strip()
    if not endpoint:
        raise RuntimeError("cloud_bridge.endpoint is empty")
    if not token:
        raise RuntimeError("cloud_bridge.token is empty")
    return endpoint, token


def main():
    config = load_config()
    bridge = config.get("cloud_bridge", {})
    endpoint, _token = validate_bridge_config(bridge)
    print(f"[cloud_bridge] posting to {endpoint}")

    last_frame_mtime = None
    while True:
        loop_started = time.time()
        config = load_config()
        bridge = config.get("cloud_bridge", {})
        interval_sec = max(2, int(bridge.get("interval_sec", DEFAULT_INTERVAL_SEC)))
        timeout_sec = max(2, int(bridge.get("timeout_sec", DEFAULT_TIMEOUT_SEC)))
        endpoint, _token = validate_bridge_config(bridge)

        try:
            latest_map = fetch_latest_map(timeout_sec)
            sign_state = fetch_sign_state(timeout_sec)
            payload, last_frame_mtime = build_payload(config, latest_map, sign_state, last_frame_mtime)
            response_text = post_payload(endpoint, timeout_sec, payload)
            print(f"[cloud_bridge] ok metrics={len(payload['metrics'])} frame={'yes' if payload.get('frame_base64') else 'no'} response={response_text[:140]}")
        except urllib.error.HTTPError as exc:
            print(f"[cloud_bridge] http error status={exc.code} reason={exc.reason}")
        except Exception as exc:
            print(f"[cloud_bridge] error {exc}")

        elapsed = time.time() - loop_started
        time.sleep(max(1, interval_sec - elapsed))


if __name__ == "__main__":
    main()
