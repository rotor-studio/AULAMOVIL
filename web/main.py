import asyncio
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import math
import struct
from array import array
from datetime import datetime, timezone

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


def load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def merge_dict(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    base = load_yaml("/opt/rotor-meteo/config/app.yaml")
    secrets = load_yaml("/etc/rotor-meteo/secrets.yaml")
    return merge_dict(base, secrets)


def parse_timestamp(value, default=None):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            v = value
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return default
    return default


def text_for_sign(value):
    text = str(value or "")
    replacements = {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "«": '"',
        "»": '"',
        "–": "-",
        "—": "-",
        "…": "...",
        "º": "o",
        "ª": "a",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(text.split())


def sanitize_filename(value):
    text = str(value or "").strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    safe = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            safe.append(ch)
        elif ch.isspace():
            safe.append("_")
    cleaned = "".join(safe).strip("._")
    return cleaned or "sound"


def unique_sound_filename(original):
    stem, ext = os.path.splitext(original)
    safe_stem = sanitize_filename(stem)
    safe_ext = ext.lower()
    candidate = f"{safe_stem}{safe_ext}"
    path = os.path.join(SOUND_DIR, candidate)
    if not os.path.exists(path):
        return candidate
    suffix = 2
    while True:
        candidate = f"{safe_stem}_{suffix}{safe_ext}"
        path = os.path.join(SOUND_DIR, candidate)
        if not os.path.exists(path):
            return candidate
        suffix += 1


CONFIG = load_config()
REGISTRY = load_yaml("/opt/rotor-meteo/config/registry.yaml")
DB_PATH = CONFIG["storage"]["sqlite_path"]
SSE_INTERVAL = float(CONFIG["web"].get("sse_interval_sec", 1.0))
CAMERA = CONFIG.get("camera", {})
HLS_DIR = CAMERA.get("hls_dir") or "/opt/rotor-meteo/data/hls"
TIMELAPSE_DIR = CAMERA.get("timelapse_dir") or "/opt/rotor-meteo/data/timelapse"
CAMERA_STALE_SEC = float(CONFIG.get("camera", {}).get("stale_sec", 20))
SOUND_DIR = "/opt/rotor-meteo/data/sounds"
SOUND_CONFIG_PATH = "/opt/rotor-meteo/data/sound_config.json"
FX_CONFIG_PATH = "/opt/rotor-meteo/data/fx_config.json"
WIND_CALIBRATION_PATH = "/opt/rotor-meteo/data/wind_calibration.json"
RAIN_WINDOW_CONFIG_PATH = "/opt/rotor-meteo/data/rain_window_config.json"
VAPOR_SEQUENCE_PATH = "/opt/rotor-meteo/data/vapor_sequence.json"
VAPOR_AUTOMATION_CONFIG_PATH = "/opt/rotor-meteo/data/vapor_automation.json"
EXPORT_DIR = "/opt/rotor-meteo/data/exports"
INTERPRETATION_MESSAGES_PATH = CONFIG.get("interpretation", {}).get(
    "messages_csv",
    "/opt/rotor-meteo/data/messages/refranes_meteorologicos_asturias.csv",
)
DEFAULT_USB_AUDIO_DEVICE = "plughw:CARD=CD002,DEV=0"
DEFAULT_JACK_AUDIO_DEVICE = "plughw:CARD=Headphones,DEV=0"
RAIN_WINDOW_SEC = 12 * 3600

def can_open_audio_device(device):
    if not device:
        return False
    try:
        result = subprocess.run(
            [
                "aplay",
                "-q",
                "-D",
                device,
                "-f",
                "S16_LE",
                "-c",
                "2",
                "-r",
                "22050",
                "-d",
                "1",
                "/dev/zero",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False

def resolve_audio_device():
    audio_device_candidates = [
        os.environ.get("ROTOR_AUDIO_DEVICE", "").strip(),
        configured_output_device(),
        DEFAULT_USB_AUDIO_DEVICE,
        "default",
        "sysdefault",
        DEFAULT_JACK_AUDIO_DEVICE,
    ]
    seen = set()
    for device in audio_device_candidates:
        if not device or device in seen:
            continue
        seen.add(device)
        if can_open_audio_device(device):
            return device
    return None

timelapse_lock = threading.Lock()
timelapse_thread = None
timelapse_stop = threading.Event()
timelapse_interval = 60
export_lock = threading.Lock()
export_thread = None
export_stop = threading.Event()
export_interval = 5
export_runtime = {
    "running": False,
    "started_at": None,
    "last_capture_at": None,
    "current_filename": None,
    "rows_written": 0,
    "columns": [],
}

# Optional human-friendly overrides
LABEL_OVERRIDES = {
    "wind_speed_ms": "Velocidad del viento",
    "wind_direction_deg": "Dirección del viento",
    "wind_direction_cardinal": "Dirección del viento (cardinal)",
    "rain_tips_total": "Lluvia (tics totales)",
    "rain_mm_total": "Lluvia acumulada",
    "rain_mm_interval": "Lluvia intervalo",
    "rain_rate_mmh": "Intensidad de lluvia",
    "rain_last_tip_ms": "Último tic (ms)",
    "rain_since_last_tip_ms": "Tiempo desde último tic (ms)",
}

app = FastAPI(title="Rotor Meteo")


@app.middleware("http")
async def disable_cache_for_dynamic_routes(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if request.method == "GET" and not (
        path.startswith("/static/")
        or path.startswith("/hls/")
        or path.startswith("/timelapse/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.mount("/static", StaticFiles(directory="/opt/rotor-meteo/web/static"), name="static")
app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")
app.mount("/timelapse", StaticFiles(directory=TIMELAPSE_DIR), name="timelapse")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def build_rtsp_url():
    host = CAMERA.get("host")
    if not host:
        return None
    user = CAMERA.get("username") or ""
    pwd = CAMERA.get("password") or ""
    path = CAMERA.get("rtsp_path") or "/Streaming/Channels/101"
    auth = ""
    if user:
        auth = f"{user}:{pwd}@" if pwd else f"{user}@"
    return f"rtsp://{auth}{host}:554{path}"


def build_hls_url():
    return "/hls/stream.m3u8"


def ensure_timelapse_dir():
    os.makedirs(TIMELAPSE_DIR, exist_ok=True)


def ensure_export_dir():
    os.makedirs(EXPORT_DIR, exist_ok=True)


EXPORT_EXCLUDED_METRICS = {
    ("bme280_local", "pressure_hpa"),
    ("gps_usb_1", "gps_lat"),
    ("gps_usb_1", "gps_lon"),
    ("gps_usb_1", "gps_alt"),
    ("rain_node_mcu", "rain_last_tip_ms"),
    ("rain_node_mcu", "rain_since_last_tip_ms"),
    ("wind_esp8266", "wind_speed_raw"),
    ("wind_esp8266", "wind_speed_ma"),
    ("wind_esp8266", "wind_direction_raw"),
    ("wind_esp8266", "wind_direction_ma"),
    ("wind_esp8266", "wind_raw"),
    ("wind_esp8266", "wind_voltage_v"),
    ("wind_esp8266", "wind_current_ma"),
    ("light_mcu", "uv_voltage_v"),
}


def export_metric_allowed(device_id, metric_id):
    return (str(device_id), str(metric_id)) not in EXPORT_EXCLUDED_METRICS


def export_metric_specs():
    specs = []
    seen = set()
    for device_id, device in (REGISTRY.get("devices") or {}).items():
        for metric_id in (device.get("metrics") or []):
            key = (str(device_id), str(metric_id))
            if not export_metric_allowed(*key):
                continue
            if key in seen:
                continue
            seen.add(key)
            specs.append(key)

    latest = get_latest_map()
    for key, item in sorted(latest.items()):
        device_id = str(item.get("device_id") or "")
        metric_id = str(item.get("metric_id") or "")
        pair = (device_id, metric_id)
        if not export_metric_allowed(*pair):
            continue
        if pair in seen:
            continue
        seen.add(pair)
        specs.append(pair)
    return specs


def export_header(specs):
    fields = [
        "captured_at_iso",
        "captured_at_epoch",
        "location",
        "gps_lat",
        "gps_lon",
        "gps_alt",
    ]
    for device_id, metric_id in specs:
        fields.append(f"{device_id}__{metric_id}")
    return fields


EXPORT_LAST_READING_FALLBACKS = {
    ("sensor_community_1", "pm10_ugm3"),
    ("sensor_community_1", "pm2_5_ugm3"),
    ("sensor_community_1", "temp_c"),
    ("sensor_community_1", "rh_pct"),
    ("sensor_community_1", "pressure_hpa"),
}


def export_metric_value(latest, device_id, metric_id, fallback_cache=None):
    item = latest.get(f"{device_id}/{metric_id}")
    if item:
        return item.get("value")
    key = (str(device_id), str(metric_id))
    if key not in EXPORT_LAST_READING_FALLBACKS:
        return ""
    cache = fallback_cache if fallback_cache is not None else {}
    if key not in cache:
        cache[key] = get_last_reading(metric_id, device_id)
    row = cache.get(key)
    return row.get("value") if row else ""


def build_export_row(specs, latest):
    captured_at = datetime.now(timezone.utc)
    gps_lat = get_metric(latest, "gps_lat", "gps_usb_1") or get_metric(latest, "gps_lat")
    gps_lon = get_metric(latest, "gps_lon", "gps_usb_1") or get_metric(latest, "gps_lon")
    gps_alt = get_metric(latest, "gps_alt", "gps_usb_1") or get_metric(latest, "gps_alt")
    lat_value = gps_lat.get("value") if gps_lat else ""
    lon_value = gps_lon.get("value") if gps_lon else ""
    alt_value = gps_alt.get("value") if gps_alt else ""
    location = ""
    if lat_value != "" and lon_value != "":
        location = f"{lat_value}, {lon_value}"

    row = {
        "captured_at_iso": captured_at.isoformat(),
        "captured_at_epoch": round(captured_at.timestamp(), 3),
        "location": location,
        "gps_lat": lat_value,
        "gps_lon": lon_value,
        "gps_alt": alt_value,
    }
    fallback_cache = {}
    for device_id, metric_id in specs:
        row[f"{device_id}__{metric_id}"] = export_metric_value(latest, device_id, metric_id, fallback_cache)
    return row


def export_loop(file_path, specs, fieldnames):
    global export_interval
    try:
        with open(file_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            f.flush()
            while not export_stop.is_set():
                latest = get_latest_map()
                row = build_export_row(specs, latest)
                writer.writerow(row)
                f.flush()
                with export_lock:
                    export_runtime["rows_written"] += 1
                    export_runtime["last_capture_at"] = row["captured_at_iso"]
                export_stop.wait(export_interval)
    finally:
        with export_lock:
            export_runtime["running"] = False


def export_running():
    return export_thread is not None and export_thread.is_alive()


def list_export_files(limit=20):
    ensure_export_dir()
    items = []
    for name in sorted(os.listdir(EXPORT_DIR), reverse=True):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(EXPORT_DIR, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        items.append({
            "filename": name,
            "size_bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "download_url": f"/api/export/download/{urllib.parse.quote(name)}",
            "delete_url": f"/api/export/files/{urllib.parse.quote(name)}",
        })
        if len(items) >= limit:
            break
    return items


def export_status():
    with export_lock:
        running = bool(export_runtime.get("running"))
        started_at = export_runtime.get("started_at")
        last_capture_at = export_runtime.get("last_capture_at")
        current_filename = export_runtime.get("current_filename")
        rows_written = int(export_runtime.get("rows_written") or 0)
        columns = list(export_runtime.get("columns") or [])
    return {
        "ok": True,
        "running": running,
        "interval_sec": int(export_interval),
        "started_at": started_at,
        "last_capture_at": last_capture_at,
        "current_filename": current_filename,
        "rows_written": rows_written,
        "column_count": len(columns),
        "columns": columns,
        "current_download_url": f"/api/export/download/{urllib.parse.quote(current_filename)}" if current_filename else None,
        "files": list_export_files(),
    }


def start_export_recording(interval_sec: int):
    global export_thread, export_interval
    ensure_export_dir()
    thread = None
    with export_lock:
        thread = export_thread if export_running() else None
    if thread and thread.is_alive():
        export_stop.set()
        thread.join(timeout=2)
    with export_lock:
        export_interval = max(1, int(interval_sec))
        export_stop.clear()
        specs = export_metric_specs()
        fieldnames = export_header(specs)
        filename = f"meteo_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        export_runtime["running"] = True
        export_runtime["started_at"] = datetime.now(timezone.utc).isoformat()
        export_runtime["last_capture_at"] = None
        export_runtime["current_filename"] = filename
        export_runtime["rows_written"] = 0
        export_runtime["columns"] = list(fieldnames)
        file_path = os.path.join(EXPORT_DIR, filename)
        export_thread = threading.Thread(
            target=export_loop,
            args=(file_path, specs, fieldnames),
            daemon=True,
        )
        export_thread.start()
    return export_status()


def stop_export_recording():
    global export_thread
    export_stop.set()
    thread = export_thread
    if thread and thread.is_alive():
        thread.join(timeout=2)
    with export_lock:
        export_runtime["running"] = False
    return export_status()


def capture_timelapse_frame():
    # Copy latest snapshot if available; fallback to fetch snapshot endpoint.
    ensure_timelapse_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(TIMELAPSE_DIR, f"frame_{ts}.jpg")
    src = os.path.join(HLS_DIR, "latest.jpg")
    try:
        if os.path.exists(src):
            with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
                fdst.write(fsrc.read())
            return dest
    except Exception:
        pass
    # If no latest.jpg, just skip
    return None


def timelapse_loop():
    global timelapse_interval
    while not timelapse_stop.is_set():
        capture_timelapse_frame()
        timelapse_stop.wait(timelapse_interval)


def timelapse_running():
    return timelapse_thread is not None and timelapse_thread.is_alive()


def start_timelapse(interval_sec: int):
    global timelapse_thread, timelapse_interval
    with timelapse_lock:
        timelapse_interval = max(1, int(interval_sec))
        if timelapse_running():
            return
        timelapse_stop.clear()
        timelapse_thread = threading.Thread(target=timelapse_loop, daemon=True)
        timelapse_thread.start()


def stop_timelapse():
    with timelapse_lock:
        timelapse_stop.set()


def list_timelapse(offset=0, limit=40):
    ensure_timelapse_dir()
    files = [f for f in os.listdir(TIMELAPSE_DIR) if f.lower().endswith(".jpg")]
    files.sort(reverse=True)
    return files[offset: offset + limit]


def clear_timelapse():
    ensure_timelapse_dir()
    for f in os.listdir(TIMELAPSE_DIR):
        if f.lower().endswith(".jpg") or f.lower().endswith(".gif"):
            try:
                os.remove(os.path.join(TIMELAPSE_DIR, f))
            except Exception:
                pass


def create_timelapse_gif(frame_interval_sec: float = 1.0):
    try:
        from PIL import Image
    except Exception:
        return None
    ensure_timelapse_dir()
    files = [f for f in os.listdir(TIMELAPSE_DIR) if f.lower().endswith(".jpg")]
    files.sort()
    if not files:
        return None
    frames = []
    for f in files:
        try:
            img = Image.open(os.path.join(TIMELAPSE_DIR, f)).convert("RGB")
            frames.append(img)
        except Exception:
            continue
    if not frames:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(TIMELAPSE_DIR, f"timelapse_{ts}.gif")
    duration_ms = max(1, int(frame_interval_sec * 1000))
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
    )
    return os.path.basename(out)


def get_metric_name(metric_id):
    if metric_id in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[metric_id]
    return REGISTRY.get("metrics", {}).get(metric_id, {}).get("name", metric_id)


def get_metric_unit(metric_id, fallback=None):
    return REGISTRY.get("metrics", {}).get(metric_id, {}).get("unit", fallback)


def get_latest_map():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM latest").fetchall()
    conn.close()
    out = {}
    for r in rows:
        key = f"{r['device_id']}/{r['metric_id']}"
        out[key] = {
            "device_id": r["device_id"],
            "metric_id": r["metric_id"],
            "ts": r["ts"],
            "value": r["value"],
            "unit": r["unit"],
        }
    return out


def get_metric(latest_map, metric_id, device_id=None):
    rows = [v for v in latest_map.values() if v["metric_id"] == metric_id]
    if device_id:
        rows = [v for v in rows if v["device_id"] == device_id]
    if not rows:
        return None
    rows.sort(key=lambda item: item.get("ts") or 0, reverse=True)
    return rows[0]


def get_history(metric_id, device_id=None, seconds=21600):
    end = time.time()
    start = end - seconds
    conn = get_conn()
    if device_id:
        rows = conn.execute(
            "SELECT ts, value, unit, device_id, metric_id FROM readings WHERE metric_id=? AND device_id=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
            (metric_id, device_id, start, end),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, value, unit, device_id, metric_id FROM readings WHERE metric_id=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
            (metric_id, start, end),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def default_rain_window_config():
    return {
        "anchor_ts": None,
        "updated_at": None,
    }


def load_rain_window_config():
    if not os.path.exists(RAIN_WINDOW_CONFIG_PATH):
        return default_rain_window_config()
    try:
        with open(RAIN_WINDOW_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_rain_window_config()
        config = default_rain_window_config()
        config.update(data)
        anchor_ts = config.get("anchor_ts")
        config["anchor_ts"] = float(anchor_ts) if anchor_ts is not None else None
        return config
    except Exception:
        return default_rain_window_config()


def save_rain_window_config(config):
    os.makedirs(os.path.dirname(RAIN_WINDOW_CONFIG_PATH), exist_ok=True)
    payload = default_rain_window_config()
    payload.update(config or {})
    with open(RAIN_WINDOW_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def default_rain_window_start(now_ts=None):
    now_dt = datetime.fromtimestamp(now_ts or time.time(), tz=timezone.utc).astimezone()
    if now_dt.hour < 12:
        start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = now_dt.replace(hour=12, minute=0, second=0, microsecond=0)
    return start_dt.timestamp()


def current_rain_window_bounds(now_ts=None, config=None):
    now_ts = float(now_ts or time.time())
    config = config or load_rain_window_config()
    anchor_ts = config.get("anchor_ts")
    if anchor_ts is not None and anchor_ts <= now_ts:
        elapsed = max(0.0, now_ts - float(anchor_ts))
        window_index = int(elapsed // RAIN_WINDOW_SEC)
        start_ts = float(anchor_ts) + (window_index * RAIN_WINDOW_SEC)
        source = "manual"
    else:
        start_ts = default_rain_window_start(now_ts)
        source = "default"
    end_ts = start_ts + RAIN_WINDOW_SEC
    return {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "source": source,
        "anchor_ts": anchor_ts,
    }


def get_last_reading(metric_id, device_id=None, at_or_before=None, at_or_after=None):
    conn = get_conn()
    clauses = ["metric_id=?"]
    params = [metric_id]
    if device_id:
        clauses.append("device_id=?")
        params.append(device_id)
    if at_or_before is not None:
        clauses.append("ts<=?")
        params.append(float(at_or_before))
        order = "ORDER BY ts DESC"
    elif at_or_after is not None:
        clauses.append("ts>=?")
        params.append(float(at_or_after))
        order = "ORDER BY ts ASC"
    else:
        order = "ORDER BY ts DESC"
    query = (
        "SELECT ts, value, unit, device_id, metric_id FROM readings "
        f"WHERE {' AND '.join(clauses)} {order} LIMIT 1"
    )
    row = conn.execute(query, tuple(params)).fetchone()
    conn.close()
    return dict(row) if row else None


def compute_rain_window_state(now_ts=None):
    now_ts = float(now_ts or time.time())
    window = current_rain_window_bounds(now_ts)
    latest = get_latest_map()
    total_now = get_metric(latest, "rain_mm_total", "rain_node_mcu") or get_metric(latest, "rain_mm", "rain_node_mcu")
    tips_now = get_metric(latest, "rain_tips_total", "rain_node_mcu")
    baseline_total = get_last_reading("rain_mm_total", "rain_node_mcu", at_or_before=window["start_ts"])
    if not baseline_total:
        baseline_total = get_last_reading("rain_mm_total", "rain_node_mcu", at_or_after=window["start_ts"])
    baseline_tips = get_last_reading("rain_tips_total", "rain_node_mcu", at_or_before=window["start_ts"])
    if not baseline_tips:
        baseline_tips = get_last_reading("rain_tips_total", "rain_node_mcu", at_or_after=window["start_ts"])

    total_now_val = metric_float(total_now)
    tips_now_val = metric_float(tips_now)
    baseline_total_val = metric_float(baseline_total)
    baseline_tips_val = metric_float(baseline_tips)

    window_total = None
    if total_now_val is not None:
        if baseline_total_val is None:
            window_total = max(0.0, total_now_val)
        elif total_now_val >= baseline_total_val:
            window_total = total_now_val - baseline_total_val
        else:
            window_total = max(0.0, total_now_val)

    window_tips = None
    if tips_now_val is not None:
        if baseline_tips_val is None:
            window_tips = max(0.0, tips_now_val)
        elif tips_now_val >= baseline_tips_val:
            window_tips = tips_now_val - baseline_tips_val
        else:
            window_tips = max(0.0, tips_now_val)

    return {
        "window_start_ts": window["start_ts"],
        "window_end_ts": window["end_ts"],
        "next_reset_ts": window["end_ts"],
        "window_source": window["source"],
        "anchor_ts": window["anchor_ts"],
        "window_total_mm": window_total,
        "window_tips_total": window_tips,
        "current_total_mm": total_now_val,
        "current_tips_total": tips_now_val,
        "baseline_total_mm": baseline_total_val,
        "baseline_tips_total": baseline_tips_val,
        "current_rate_mmh": metric_float(get_metric(latest, "rain_rate_mmh", "rain_node_mcu")),
        "ts": total_now.get("ts") if total_now else (tips_now.get("ts") if tips_now else None),
    }


INTERPRETATION_HISTORY_BUCKET_SEC = 180
INTERPRETATION_HISTORY_RETENTION_SEC = 21 * 24 * 3600
INTERPRETATION_HISTORY_LIMIT = 8
_interpretation_history_ready = False
_interpretation_history_lock = threading.Lock()


def ensure_interpretation_history_table():
    global _interpretation_history_ready
    if _interpretation_history_ready:
        return
    with _interpretation_history_lock:
        if _interpretation_history_ready:
            return
        conn = get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interpretation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_key TEXT NOT NULL UNIQUE,
                ts REAL NOT NULL,
                source_ts REAL,
                state_id TEXT NOT NULL,
                state_score REAL,
                confidence TEXT,
                label TEXT,
                summary TEXT,
                detail TEXT,
                phrase TEXT NOT NULL,
                message_condition TEXT,
                message_category TEXT,
                message_priority INTEGER,
                ranked_json TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interpretation_history_ts ON interpretation_history(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interpretation_history_state_ts ON interpretation_history(state_id, ts DESC)")
        conn.commit()
        conn.close()
        _interpretation_history_ready = True


def get_recent_interpretation_history(limit=INTERPRETATION_HISTORY_LIMIT, seconds=12 * 3600):
    ensure_interpretation_history_table()
    conn = get_conn()
    cutoff = time.time() - seconds
    rows = conn.execute(
        """
        SELECT ts, source_ts, state_id, state_score, confidence, label, summary, detail,
               phrase, message_condition, message_category, message_priority, ranked_json
        FROM interpretation_history
        WHERE ts >= ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (cutoff, int(limit)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_interpretation_history(entry):
    ensure_interpretation_history_table()
    ts = float(entry.get("ts") or time.time())
    state_id = str(entry.get("state_id") or "mensaje_general")
    phrase = str(entry.get("phrase") or "").strip()
    if not phrase:
        return
    bucket = int(ts // INTERPRETATION_HISTORY_BUCKET_SEC)
    bucket_key = f"{bucket}:{state_id}:{phrase}"
    conn = get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO interpretation_history (
            bucket_key, ts, source_ts, state_id, state_score, confidence,
            label, summary, detail, phrase, message_condition,
            message_category, message_priority, ranked_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bucket_key,
            ts,
            entry.get("source_ts"),
            state_id,
            entry.get("state_score"),
            entry.get("confidence"),
            entry.get("label"),
            entry.get("summary"),
            entry.get("detail"),
            phrase,
            entry.get("message_condition"),
            entry.get("message_category"),
            entry.get("message_priority"),
            entry.get("ranked_json"),
        ),
    )
    conn.execute(
        "DELETE FROM interpretation_history WHERE ts < ?",
        (time.time() - INTERPRETATION_HISTORY_RETENTION_SEC,),
    )
    conn.commit()
    conn.close()


def get_recent_message_phrases(seconds=12 * 3600, limit=24):
    rows = get_recent_interpretation_history(limit=limit, seconds=seconds)
    return [str(row.get("phrase") or "").strip() for row in rows if row.get("phrase")]


def sample_before(points, seconds_ago):
    if not points:
        return None
    cutoff = time.time() - seconds_ago
    candidate = None
    for item in points:
        if item["ts"] <= cutoff:
            candidate = item
        else:
            break
    return candidate or points[0]


_message_cache = {"path": None, "mtime": None, "rows": []}
_message_cache_lock = threading.Lock()


def metric_float(item):
    if not item:
        return None
    try:
        return float(item["value"])
    except (TypeError, ValueError):
        return None


def load_interpretation_messages():
    path = INTERPRETATION_MESSAGES_PATH
    if not os.path.exists(path):
        return []
    mtime = os.path.getmtime(path)
    with _message_cache_lock:
        if _message_cache["path"] == path and _message_cache["mtime"] == mtime:
            return list(_message_cache["rows"])

        rows = []
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                condition = (row.get("condicion_logica") or "").strip()
                phrase = (row.get("frase") or "").strip()
                if not condition or not phrase:
                    continue
                try:
                    priority = int(row.get("prioridad_1_5") or 1)
                except ValueError:
                    priority = 1
                rows.append({
                    "category": (row.get("categoria") or "").strip(),
                    "sensor_parameters": (row.get("parametro_sensor") or "").strip(),
                    "condition": condition,
                    "phrase": phrase,
                    "type": (row.get("tipo") or "").strip(),
                    "source": (row.get("procedencia") or "").strip(),
                    "source_url": (row.get("fuente_url") or "").strip(),
                    "notes": (row.get("notas_de_uso") or "").strip(),
                    "priority": max(1, min(priority, 5)),
                })
        _message_cache.update({"path": path, "mtime": mtime, "rows": rows})
        return list(rows)


def confidence_from_score(score, observed_count):
    if observed_count < 3 or score < 3:
        return "baja"
    if score >= 8 and observed_count >= 5:
        return "alta"
    return "media"


NON_VARIANT_CATEGORIES = {
    "Interfaz / meta",
    "Refranero / contexto",
}


STATE_VARIANT_FALLBACKS = {
    "aire_limpio": [
        "El aire va limpio y ligero.",
        "Respirar hoy no cuesta.",
        "Ambiente limpio, sin carga.",
    ],
    "presion_estable": [
        "La presión se mantiene firme.",
        "Tiempo sereno, sin sobresaltos.",
        "La atmósfera sigue en pausa.",
    ],
    "mensaje_general": [
        "El tiempo va por su sitio.",
        "La estación lee el aire sin prisa.",
        "Se mantiene un pulso tranquilo.",
    ],
}


def choose_candidate(rows, seed_text, avoid_phrases=None):
    if not rows:
        return None
    candidates = sorted(rows, key=lambda row: (-row["priority"], row["phrase"]))
    top_priority = candidates[0]["priority"]
    best = [row for row in candidates if row["priority"] == top_priority]
    avoid = {str(item).strip() for item in (avoid_phrases or []) if str(item).strip()}
    if avoid:
        fresh = [row for row in best if row["phrase"] not in avoid]
        if fresh:
            best = fresh
    digest = hashlib.sha1(seed_text.encode("utf-8")).hexdigest()
    return best[int(digest[:8], 16) % len(best)]


def choose_message(rows, condition, seed_text, avoid_phrases=None):
    candidates = [row for row in rows if row["condition"] == condition]
    if candidates:
        chosen = choose_candidate(candidates, seed_text, avoid_phrases)
        if chosen is not None:
            chosen_phrase = str(chosen.get("phrase") or "").strip()
            if chosen_phrase and chosen_phrase in {str(item).strip() for item in (avoid_phrases or [])}:
                fallback_phrases = STATE_VARIANT_FALLBACKS.get(condition, [])
                if fallback_phrases:
                    digest = hashlib.sha1(seed_text.encode("utf-8")).hexdigest()
                    return {
                        "category": "Sistema",
                        "sensor_parameters": "",
                        "condition": condition,
                        "phrase": fallback_phrases[int(digest[:8], 16) % len(fallback_phrases)],
                        "type": "variant",
                        "source": "",
                        "source_url": "",
                        "notes": "variante local para evitar repetición",
                        "priority": chosen.get("priority", 1),
                    }
            return chosen

    candidates = [row for row in rows if row["condition"] in ("mensaje_general", "uso_general_refranero")]
    if not candidates:
        fallback_phrases = STATE_VARIANT_FALLBACKS.get(condition, [])
        if fallback_phrases:
            digest = hashlib.sha1(seed_text.encode("utf-8")).hexdigest()
            return {
                "category": "Sistema",
                "sensor_parameters": "",
                "condition": condition,
                "phrase": fallback_phrases[int(digest[:8], 16) % len(fallback_phrases)],
                "type": "variant",
                "source": "",
                "source_url": "",
                "notes": "variante local para evitar repetición",
                "priority": 1,
            }
        return None
    return choose_candidate(candidates, seed_text, avoid_phrases)


def build_message_seed(state_id, state_score, confidence, ranked, source_ts):
    top_conditions = "|".join(
        f"{condition}:{round(score, 1)}"
        for condition, score in ranked[:3]
    )
    base = f"{state_id}:{round(state_score, 2)}:{confidence}:{top_conditions}"
    return f"{base}:{int(source_ts // 180)}"


def build_rank_signature(ranked, limit=3):
    return "|".join(
        f"{condition}:{round(score, 1)}"
        for condition, score in ranked[:limit]
    )


def parse_rank_signature(raw_ranked_json):
    if not raw_ranked_json:
        return ""
    try:
        ranked = json.loads(raw_ranked_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    cleaned = []
    for item in ranked[:3]:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                cleaned.append((str(item[0]), float(item[1])))
            except (TypeError, ValueError):
                continue
    return build_rank_signature(cleaned)


def choose_ranked_message_condition(primary_state_id, ranked, history_rows, now_ts):
    rotation = []
    for condition, score in ranked:
        if score < 2:
            continue
        if condition == "mensaje_general":
            continue
        rotation.append(condition)
        if len(rotation) >= 3:
            break
    if not rotation:
        rotation = [primary_state_id]
    elif primary_state_id not in rotation:
        rotation.insert(0, primary_state_id)
    else:
        rotation = [primary_state_id] + [item for item in rotation if item != primary_state_id]

    if len(rotation) == 1:
        return primary_state_id

    current_signature = build_rank_signature(ranked)
    anchor_ts = None
    for row in reversed(history_rows):
        if str(row.get("state_id") or "") != primary_state_id:
            break
        if parse_rank_signature(row.get("ranked_json")) != current_signature:
            break
        try:
            anchor_ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue

    if anchor_ts is None:
        return primary_state_id

    elapsed = max(0.0, now_ts - anchor_ts)
    if elapsed < 180:
        return primary_state_id

    cycle_index = int(elapsed // 180) % len(rotation)
    return rotation[cycle_index]


def compute_local_24h_interpretation():
    latest = get_latest_map()
    messages = load_interpretation_messages()
    recent_phrases = get_recent_message_phrases()
    recent_history = get_recent_interpretation_history(limit=18, seconds=12 * 3600)

    pressure = get_metric(latest, "pressure_hpa", "sensor_community_1")
    humidity = get_metric(latest, "rh_pct", "sensor_community_1")
    temperature = get_metric(latest, "temp_c", "sensor_community_1")
    wind = get_metric(latest, "wind_speed_ms", "wind_esp8266")
    rain_rate = get_metric(latest, "rain_rate_mmh", "rain_node_mcu")
    rain_total = get_metric(latest, "rain_mm_total", "rain_node_mcu") or get_metric(latest, "rain_mm", "rain_node_mcu")
    light = get_metric(latest, "light_lux", "light_mcu")
    uv_raw = get_metric(latest, "uv_raw", "light_mcu")
    uv_voltage = get_metric(latest, "uv_voltage_v", "light_mcu")
    pm10 = get_metric(latest, "pm10_ugm3", "sensor_community_1")
    pm25 = get_metric(latest, "pm2_5_ugm3", "sensor_community_1")
    ground_temp = get_metric(latest, "temp_ground_c", "bme280_ground")
    ground_humidity = get_metric(latest, "rh_ground_pct", "bme280_ground")

    pressure_series = get_history("pressure_hpa", "sensor_community_1")
    humidity_series = get_history("rh_pct", "sensor_community_1")
    rain_series = get_history("rain_mm_total", "rain_node_mcu")
    light_series = get_history("light_lux", "light_mcu")
    pm25_series = get_history("pm2_5_ugm3", "sensor_community_1")

    pressure_prev = sample_before(pressure_series, 6 * 3600)
    humidity_prev = sample_before(humidity_series, 6 * 3600)
    rain_prev = sample_before(rain_series, 6 * 3600)
    light_prev = sample_before(light_series, 3 * 3600)
    pm25_prev = sample_before(pm25_series, 6 * 3600)

    pressure_val = metric_float(pressure)
    humidity_val = metric_float(humidity)
    temp_val = metric_float(temperature)
    wind_val = metric_float(wind)
    rain_rate_val = metric_float(rain_rate)
    rain_total_val = metric_float(rain_total)
    light_val = metric_float(light)
    uv_raw_val = metric_float(uv_raw)
    uv_voltage_val = metric_float(uv_voltage)
    pm10_val = metric_float(pm10)
    pm25_val = metric_float(pm25)
    ground_temp_val = metric_float(ground_temp)
    ground_humidity_val = metric_float(ground_humidity)

    pressure_trend = pressure_val - metric_float(pressure_prev) if pressure_val is not None and pressure_prev else None
    humidity_trend = humidity_val - metric_float(humidity_prev) if humidity_val is not None and humidity_prev else None
    rain_delta = rain_total_val - metric_float(rain_prev) if rain_total_val is not None and rain_prev else None
    light_trend = light_val - metric_float(light_prev) if light_val is not None and light_prev else None
    pm25_trend = pm25_val - metric_float(pm25_prev) if pm25_val is not None and pm25_prev else None

    scores = {}
    evidence = {}

    def add(condition, points, reason):
        scores[condition] = scores.get(condition, 0.0) + points
        evidence.setdefault(condition, []).append(reason)

    now_dt = datetime.now()
    month = now_dt.month

    if rain_rate_val is not None:
        if rain_rate_val >= 8:
            add("lluvia_fuerte", 6, f"intensidad de lluvia {rain_rate_val:.2f} mm/h")
        elif rain_rate_val >= 2:
            add("dia_lluvioso", 5, f"lluvia activa {rain_rate_val:.2f} mm/h")
            add("lluvia_continua", 3, f"lluvia activa {rain_rate_val:.2f} mm/h")
        elif rain_rate_val >= 0.1:
            add("lluvia_suave", 5, f"lluvia suave {rain_rate_val:.2f} mm/h")
            add("llovizna_fina", 3, f"lluvia débil {rain_rate_val:.2f} mm/h")
    if rain_delta is not None and rain_delta >= 0.2:
        add("lluvia_beneficiosa", 2, f"acumulado reciente +{rain_delta:.2f} mm")
        add("lluvia_suave_y_humedad_alta", 2, f"lluvia reciente +{rain_delta:.2f} mm")
        if temp_val is not None and temp_val <= 8:
            add("lluvia_y_frio", 4, f"lluvia reciente y {temp_val:.1f} C")
        if light_val is not None and light_val >= 900:
            add("sol_despues_de_lluvia", 4, f"luz alta tras lluvia reciente")
            add("radiacion_alta_tras_lluvia", 2, f"luz alta tras lluvia reciente")

    if humidity_val is not None:
        if humidity_val >= 90:
            add("humedad_alta", 4, f"humedad {humidity_val:.0f} %")
            add("humedad_muy_alta_sin_lluvia", 2, f"humedad {humidity_val:.0f} %")
            add("niebla_y_humedad_alta", 2, f"humedad muy alta")
        elif humidity_val >= 78:
            add("humedad_alta", 3, f"humedad {humidity_val:.0f} %")
            add("ambiente_pegajoso", 2, f"humedad {humidity_val:.0f} %")
        elif humidity_val <= 45:
            add("humedad_baja", 3, f"humedad {humidity_val:.0f} %")
            add("frio_seco" if temp_val is not None and temp_val <= 10 else "viento_seco", 1, f"humedad baja")
    if humidity_trend is not None:
        if humidity_trend >= 8:
            add("humedad_subiendo", 3, f"humedad sube {humidity_trend:.1f} puntos en 6 h")
        elif humidity_trend <= -8:
            add("humedad_bajando", 3, f"humedad baja {abs(humidity_trend):.1f} puntos en 6 h")

    if pressure_val is not None:
        if pressure_val <= 1002:
            add("presion_muy_baja", 5, f"presión {pressure_val:.1f} hPa")
        elif pressure_val <= 1008:
            add("presion_baja_y_humedad_alta", 2, f"presión baja {pressure_val:.1f} hPa")
        elif pressure_val >= 1022:
            add("alta_presion_y_sol", 2, f"presión alta {pressure_val:.1f} hPa")
        else:
            add("presion_estable", 1, f"presión {pressure_val:.1f} hPa")
    if pressure_trend is not None:
        if pressure_trend <= -3:
            add("presion_bajando_rapido", 5, f"presión baja {abs(pressure_trend):.1f} hPa en 6 h")
            add("nubosidad_creciente", 2, f"presión en descenso")
            if humidity_val is not None and humidity_val >= 75:
                add("presion_baja_y_humedad_alta", 4, f"presión baja y humedad alta")
                add("nubes_densas_y_presion_baja", 3, f"presión baja y humedad alta")
        elif pressure_trend >= 3:
            add("presion_subiendo", 5, f"presión sube {pressure_trend:.1f} hPa en 6 h")
            add("alta_presion_y_sol", 2, f"presión subiendo")
        else:
            add("presion_estable", 2, f"presión estable en 6 h")

    if temp_val is not None:
        if temp_val <= 0:
            add("temperatura_bajo_cero", 5, f"temperatura {temp_val:.1f} C")
            add("hielo", 3, f"temperatura bajo cero")
        elif temp_val <= 4:
            add("helada_matinal", 3, f"temperatura {temp_val:.1f} C")
            add("frio_humedo" if humidity_val and humidity_val >= 75 else "frio_seco", 2, f"frío local")
        elif temp_val <= 9:
            add("frio_humedo" if humidity_val and humidity_val >= 75 else "frio_seco", 3, f"temperatura {temp_val:.1f} C")
        elif temp_val >= 30:
            add("calor_anomalo", 4, f"temperatura {temp_val:.1f} C")
            add("sol_y_calor", 2, f"temperatura alta")
        elif temp_val >= 25:
            add("sol_fuerte_o_calor", 3, f"temperatura {temp_val:.1f} C")
            if humidity_val is not None and humidity_val <= 50:
                add("calor_y_baja_humedad", 3, f"calor y humedad baja")

    if wind_val is not None:
        if wind_val >= 14:
            add("viento_muy_fuerte", 6, f"viento {wind_val:.1f} m/s")
            add("rachas", 3, f"viento fuerte")
        elif wind_val >= 8:
            add("viento_fuerte", 5, f"viento {wind_val:.1f} m/s")
            add("viento_persistente", 2, f"viento moderado")
        elif wind_val >= 3:
            add("brisa_suave", 2, f"brisa {wind_val:.1f} m/s")
        if rain_rate_val is not None and rain_rate_val >= 0.1 and wind_val >= 5:
            add("lluvia_y_viento", 5, f"lluvia y viento {wind_val:.1f} m/s")
        if pressure_val is not None and pressure_val <= 1008 and wind_val >= 6:
            add("presion_baja_y_viento", 4, f"presión baja y viento")

    if light_val is not None:
        if light_val >= 900:
            add("dia_luminoso", 4, f"luminosidad {light_val:.0f} lux")
            add("sol_suave", 3, f"luz alta calibrada")
        elif light_val >= 120:
            add("sol_suave", 2, f"luz interior/ambiente {light_val:.0f} lux")
            add("claros_entre_nubes", 2, f"luz moderada")
        elif light_val <= 10:
            add("cielo_gris_estable", 2, f"sensores casi a oscuras {light_val:.0f} lux")
            if humidity_val is not None and humidity_val >= 80:
                add("niebla_densa_o_dia_gris", 3, f"luz baja y humedad alta")
                add("cielo_bajo_cubierto", 2, f"luz baja y humedad alta")
        else:
            add("cielo_gris_estable", 1, f"luminosidad baja {light_val:.0f} lux")
    if light_trend is not None:
        if light_trend >= 8000:
            add("radiacion_subiendo", 2, "luminosidad subiendo")
        elif light_trend <= -8000:
            add("radiacion_bajando", 2, "luminosidad bajando")

    if uv_raw_val is not None:
        uv_evidence = f"UV raw {uv_raw_val:.0f}"
        if uv_voltage_val is not None:
            uv_evidence += f" ({uv_voltage_val:.2f} V)"
        uv_sensor_delta = abs(uv_raw_val - 325.0)
        if uv_sensor_delta <= 8:
            add("radiacion_baja", 0.25, f"{uv_evidence}; sensor UV sin variación útil")
        elif light_val is not None and light_val >= 900 and uv_sensor_delta >= 40:
            add("uv_medio", 1, f"{uv_evidence}; pendiente de recalibrar")

    if pm25_val is not None or pm10_val is not None:
        if (pm25_val is not None and pm25_val > 55) or (pm10_val is not None and pm10_val > 100):
            add("aire_malo", 5, "partículas altas")
            add("humo_o_combustion", 2, "partículas muy altas")
        elif (pm25_val is not None and pm25_val > 35) or (pm10_val is not None and pm10_val > 50):
            add("aire_malo", 4, "partículas elevadas")
            if wind_val is not None and wind_val < 2:
                add("pm_alta_y_poco_viento", 3, "partículas altas y poco viento")
        elif (pm25_val is not None and pm25_val > 12) or (pm10_val is not None and pm10_val > 20):
            add("aire_regular_o_pm_alta", 3, "aire regular")
        else:
            add("aire_limpio", 3, "partículas bajas")
    if pm25_trend is not None:
        if pm25_trend >= 5:
            add("particulas_subiendo", 2, f"PM2.5 sube {pm25_trend:.1f}")
        elif pm25_trend <= -5:
            add("particulas_bajando", 2, f"PM2.5 baja {abs(pm25_trend):.1f}")

    if ground_humidity_val is not None and ground_humidity_val >= 80:
        add("rocio", 2, f"humedad de suelo/sombra {ground_humidity_val:.0f} %")
        add("condensacion", 1, f"humedad de suelo/sombra alta")
    if ground_temp_val is not None and ground_temp_val <= 1:
        add("helada_matinal", 2, f"temperatura suelo {ground_temp_val:.1f} C")

    if month in (12, 1, 2) and temp_val is not None:
        if temp_val <= 5:
            add("enero_febrero_frio" if month in (1, 2) else "frio_inesperado", 1, "contexto invernal")
        elif temp_val >= 14:
            add("invierno_suave_anomalo", 1, "temperatura suave en invierno")
    if month in (3, 4, 5) and rain_delta is not None and rain_delta >= 0.2:
        add("primavera_lluviosa", 1, "lluvia en primavera")
    if month in (9, 10, 11) and humidity_val is not None and humidity_val >= 80:
        add("otoño_humedo", 1, "humedad alta en otoño")

    if not scores:
        add("mensaje_general", 1, "sin suficientes señales medibles")

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    state_id, state_score = ranked[0]
    observed_items = [pressure_val, humidity_val, temp_val, wind_val, rain_rate_val, rain_total_val, light_val, uv_raw_val, uv_voltage_val, pm10_val, pm25_val, ground_temp_val, ground_humidity_val]
    observed_count = sum(1 for value in observed_items if value is not None)
    confidence = confidence_from_score(state_score, observed_count)
    now_ts = time.time()
    message_condition = choose_ranked_message_condition(state_id, ranked, recent_history, now_ts)
    message_score = next((score for condition, score in ranked if condition == message_condition), state_score)
    seed = build_message_seed(message_condition, message_score, confidence, ranked, now_ts)
    message = choose_message(messages, message_condition, seed, recent_phrases)

    fallback_text = "La estación lee el tiempo de cerca: hacen falta más señales para afinar el mensaje."
    phrase = message["phrase"] if message else fallback_text
    reasons = evidence.get(message_condition) or evidence.get(state_id, [])
    summary = reasons[0] if reasons else "estado local calculado con las señales disponibles"
    detail = " / ".join(reasons[:3]) if reasons else "Sin suficientes señales específicas."
    line1 = phrase
    line2 = summary
    if len(line1) > 96:
        line1 = line1[:93] + "..."
    if len(line2) > 64:
        line2 = line2[:61] + "..."

    source_ts = max([
        item["ts"] for item in [pressure, humidity, temperature, wind, rain_rate, rain_total, light, uv_raw, uv_voltage, pm10, pm25, ground_temp, ground_humidity] if item
    ], default=0)

    record_interpretation_history({
        "ts": time.time(),
        "source_ts": source_ts,
        "state_id": state_id,
        "state_score": round(state_score, 2),
        "confidence": confidence,
        "label": state_id.replace("_", " "),
        "summary": summary,
        "detail": detail,
        "phrase": phrase,
        "message_condition": message["condition"] if message else message_condition,
        "message_category": message["category"] if message else None,
        "message_priority": message["priority"] if message else None,
        "ranked_json": json.dumps(ranked[:8], ensure_ascii=False, separators=(",", ":")),
    })

    return {
        "ok": True,
        "ts": time.time(),
        "source_ts": source_ts,
        "horizon": "24h",
        "messages": {
            "path": INTERPRETATION_MESSAGES_PATH,
            "loaded": len(messages),
        },
        "state": {
            "id": state_id,
            "score": round(state_score, 2),
            "confidence": confidence,
            "summary": summary,
            "detail": detail,
            "evidence": reasons,
        },
        "message": message or {
            "category": "Sistema",
            "sensor_parameters": "",
            "condition": state_id,
            "phrase": fallback_text,
            "type": "fallback",
            "source": "",
            "source_url": "",
            "notes": "",
            "priority": 1,
        },
        "ranked_conditions": [
            {
                "condition": condition,
                "score": round(score, 2),
                "evidence": evidence.get(condition, [])[:3],
            }
            for condition, score in ranked[:8]
        ],
        "history": get_recent_interpretation_history(),
        "metrics": {
            "temp_c": temp_val,
            "rh_pct": humidity_val,
            "pressure_hpa": pressure_val,
            "pressure_trend_6h": pressure_trend,
            "humidity_trend_6h": humidity_trend,
            "wind_speed_ms": wind_val,
            "rain_rate_mmh": rain_rate_val,
            "rain_mm_total": rain_total_val,
            "rain_delta_6h": rain_delta,
            "light_lux": light_val,
            "uv_raw": uv_raw_val,
            "uv_voltage_v": uv_voltage_val,
            "pm10_ugm3": pm10_val,
            "pm2_5_ugm3": pm25_val,
            "ground_temp_c": ground_temp_val,
            "ground_rh_pct": ground_humidity_val,
        },
        "display": {
            "headline": "Interpretación local 24h",
            "line1": line1,
            "line2": line2,
            "brightness": 64,
        },
    }


def get_actuator_config(name):
    config = load_config().get(name, {})
    return {
        "enabled": bool(config.get("enabled", False)),
        "base_url": str(config.get("base_url") or "").rstrip("/"),
        "token": str(config.get("token") or ""),
        "request_timeout_sec": max(1, int(config.get("request_timeout_sec", 4))),
        "poll_interval_sec": max(2, int(config.get("poll_interval_sec", 5))),
    }


def actuator_unavailable_state(reason="not_configured"):
    return {
        "ok": False,
        "configured": False,
        "online": False,
        "relay_on": False,
        "reason": reason,
    }


def actuator_request(method, config_name, path, fields=None):
    actuator = get_actuator_config(config_name)
    if not actuator["enabled"] or not actuator["base_url"] or not actuator["token"]:
        return actuator_unavailable_state()

    url = actuator["base_url"] + path
    body = None
    headers = {
        "X-Api-Token": actuator["token"],
    }

    payload_fields = dict(fields or {})
    if method.upper() == "GET":
        payload_fields["token"] = actuator["token"]
        query = urllib.parse.urlencode(payload_fields)
        if query:
            url += "?" + query
    else:
        payload_fields["token"] = actuator["token"]
        body = urllib.parse.urlencode(payload_fields).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=actuator["request_timeout_sec"]) as response:
            data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError("invalid actuator response")
            data.setdefault("configured", True)
            data.setdefault("online", True)
            data.setdefault("relay_on", False)
            data.setdefault("ok", True)
            return data
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "configured": True,
            "online": False,
            "relay_on": False,
            "reason": f"http_{exc.code}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "configured": True,
            "online": False,
            "relay_on": False,
            "reason": str(exc),
        }


def vapor_request(method, path, fields=None):
    return actuator_request(method, "vapor", path, fields)


def fan_request(method, path, fields=None):
    return actuator_request(method, "fan", path, fields)


def smoke_request(method, path, fields=None):
    return actuator_request(method, "smoke", path, fields)


def default_sequence_id():
    return "default"


def default_vapor_sequence_item(sequence_id=None, name="Secuencia 1"):
    sequence_id = str(sequence_id or default_sequence_id()).strip() or default_sequence_id()
    return {
        "id": sequence_id,
        "name": str(name or "Secuencia").strip() or "Secuencia",
        "initial": {
            "vapor": False,
            "fan": False,
            "smoke": False,
        },
        "steps": [],
        "duration_sec": 0.0,
        "created_at": None,
        "updated_at": None,
        "last_played_at": None,
    }


def default_vapor_sequence_store():
    default_sequence = default_vapor_sequence_item()
    return {
        "version": 2,
        "active_sequence_id": default_sequence["id"],
        "sequences": [default_sequence],
    }


def normalize_vapor_sequence_item(sequence, fallback_id=None, fallback_name=None):
    payload = default_vapor_sequence_item(
        sequence_id=fallback_id,
        name=fallback_name or "Secuencia",
    )
    payload.update(sequence or {})
    payload["id"] = str(payload.get("id") or fallback_id or uuid.uuid4().hex).strip() or uuid.uuid4().hex
    payload["name"] = str(payload.get("name") or fallback_name or payload["id"]).strip() or payload["id"]
    if not isinstance(payload.get("initial"), dict):
        payload["initial"] = {"vapor": False, "fan": False, "smoke": False}
    payload["initial"] = {
        "vapor": bool(payload["initial"].get("vapor", False)),
        "fan": bool(payload["initial"].get("fan", False)),
        "smoke": bool(payload["initial"].get("smoke", False)),
    }
    steps = payload.get("steps")
    payload["steps"] = steps if isinstance(steps, list) else []
    normalized_steps = []
    for step in payload.get("steps", []):
        if not isinstance(step, dict):
            continue
        actuator = str(step.get("actuator") or "").strip().lower()
        if actuator not in ("vapor", "fan", "smoke"):
            continue
        try:
            at_ms = int(round(float(step.get("at_ms", 0))))
        except Exception:
            at_ms = 0
        normalized_steps.append({
            "actuator": actuator,
            "enabled": bool(step.get("enabled", False)),
            "at_ms": max(0, at_ms),
        })
    normalized_steps.sort(key=lambda item: item["at_ms"])
    payload["steps"] = normalized_steps
    duration_ms = normalized_steps[-1]["at_ms"] if normalized_steps else 0
    payload["duration_sec"] = round(duration_ms / 1000.0, 3)
    return payload


def normalize_vapor_sequence_store(data):
    default_store = default_vapor_sequence_store()
    if not isinstance(data, dict):
        return default_store

    # Backward compatibility: old single-sequence shape.
    if "sequences" not in data and "steps" in data:
        migrated = default_vapor_sequence_item()
        migrated.update(data)
        sequence = normalize_vapor_sequence_item(migrated, fallback_id=default_sequence_id(), fallback_name="Secuencia 1")
        return {
            "version": 2,
            "active_sequence_id": sequence["id"],
            "sequences": [sequence],
        }

    store = {
        "version": 2,
        "active_sequence_id": str(data.get("active_sequence_id") or default_store["active_sequence_id"]).strip() or default_store["active_sequence_id"],
        "sequences": [],
    }
    sequences = data.get("sequences")
    if not isinstance(sequences, list):
        sequences = []
    seen_ids = set()
    for index, raw in enumerate(sequences):
        fallback_name = f"Secuencia {index + 1}"
        normalized = normalize_vapor_sequence_item(raw, fallback_name=fallback_name)
        if normalized["id"] in seen_ids:
            normalized["id"] = uuid.uuid4().hex
        seen_ids.add(normalized["id"])
        store["sequences"].append(normalized)
    if not store["sequences"]:
        store["sequences"] = list(default_store["sequences"])
    if store["active_sequence_id"] not in {item["id"] for item in store["sequences"]}:
        store["active_sequence_id"] = store["sequences"][0]["id"]
    return store


def load_vapor_sequence_store():
    if not os.path.exists(VAPOR_SEQUENCE_PATH):
        return default_vapor_sequence_store()
    try:
        with open(VAPOR_SEQUENCE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return normalize_vapor_sequence_store(data)
    except Exception:
        return default_vapor_sequence_store()


def save_vapor_sequence_store(store):
    os.makedirs(os.path.dirname(VAPOR_SEQUENCE_PATH), exist_ok=True)
    payload = normalize_vapor_sequence_store(store or {})
    with open(VAPOR_SEQUENCE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def get_sequence_from_store(store, sequence_id=None):
    store = normalize_vapor_sequence_store(store or {})
    wanted_id = str(sequence_id or store.get("active_sequence_id") or "").strip()
    for sequence in store.get("sequences", []):
        if sequence.get("id") == wanted_id:
            return sequence
    return store["sequences"][0]


def upsert_sequence_in_store(store, sequence):
    store = normalize_vapor_sequence_store(store or {})
    normalized = normalize_vapor_sequence_item(sequence)
    sequences = list(store.get("sequences", []))
    for index, item in enumerate(sequences):
        if item.get("id") == normalized["id"]:
            sequences[index] = normalized
            break
    else:
        sequences.append(normalized)
    store["sequences"] = sequences
    if normalized["id"]:
        store["active_sequence_id"] = normalized["id"]
    return store


vapor_sequence_lock = threading.Lock()
vapor_sequence_runtime = {
    "recording": False,
    "record_started_monotonic": None,
    "record_sequence_id": None,
    "playing": False,
    "play_started_monotonic": None,
    "play_sequence_id": None,
    "play_thread": None,
    "play_stop_event": threading.Event(),
}


def vapor_sequence_status(sequence_id=None):
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    with vapor_sequence_lock:
        record_started = vapor_sequence_runtime.get("record_started_monotonic")
        play_started = vapor_sequence_runtime.get("play_started_monotonic")
        recording = bool(vapor_sequence_runtime.get("recording"))
        playing = bool(vapor_sequence_runtime.get("playing"))
        record_sequence_id = vapor_sequence_runtime.get("record_sequence_id")
        play_sequence_id = vapor_sequence_runtime.get("play_sequence_id")
    now_mono = time.monotonic()
    play_elapsed_sec = round(now_mono - play_started, 2) if playing and play_started else 0.0
    current_step_index = None
    if playing and play_started and play_sequence_id == sequence.get("id"):
        elapsed_ms = max(0, int(round((now_mono - play_started) * 1000.0)))
        for index, step in enumerate(sequence.get("steps", [])):
            if elapsed_ms >= int(step.get("at_ms", 0)):
                current_step_index = index
            else:
                break
    return {
        "ok": True,
        "sequence_id": sequence.get("id"),
        "sequence_name": sequence.get("name"),
        "active_sequence_id": store.get("active_sequence_id"),
        "sequences": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "step_count": len(item.get("steps", [])),
                "duration_sec": float(item.get("duration_sec") or 0.0),
                "last_played_at": item.get("last_played_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in store.get("sequences", [])
        ],
        "recording": recording,
        "playing": playing,
        "step_count": len(sequence.get("steps", [])),
        "duration_sec": float(sequence.get("duration_sec") or 0.0),
        "created_at": sequence.get("created_at"),
        "updated_at": sequence.get("updated_at"),
        "last_played_at": sequence.get("last_played_at"),
        "initial": sequence.get("initial", {"vapor": False, "fan": False, "smoke": False}),
        "steps": sequence.get("steps", []),
        "record_elapsed_sec": round(now_mono - record_started, 2) if recording and record_started and record_sequence_id == sequence.get("id") else 0.0,
        "play_elapsed_sec": play_elapsed_sec,
        "current_step_index": current_step_index,
    }


def stop_vapor_sequence_playback():
    thread = None
    with vapor_sequence_lock:
        vapor_sequence_runtime["play_stop_event"].set()
        thread = vapor_sequence_runtime.get("play_thread")
    if thread and thread.is_alive():
        thread.join(timeout=1.0)
    with vapor_sequence_lock:
        vapor_sequence_runtime["playing"] = False
        vapor_sequence_runtime["play_started_monotonic"] = None
        vapor_sequence_runtime["play_sequence_id"] = None
        vapor_sequence_runtime["play_thread"] = None
        vapor_sequence_runtime["play_stop_event"] = threading.Event()


def start_vapor_sequence_recording(sequence_id=None):
    stop_vapor_sequence_playback()
    vapor_state = api_vapor_state()
    fan_state = api_fan_state()
    smoke_state = api_smoke_state()
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    sequence = normalize_vapor_sequence_item(sequence, fallback_id=sequence.get("id"), fallback_name=sequence.get("name"))
    sequence["initial"] = {
        "vapor": bool(vapor_state.get("relay_on", False)),
        "fan": bool(fan_state.get("relay_on", False)),
        "smoke": bool(smoke_state.get("relay_on", False)),
    }
    sequence["steps"] = []
    sequence["duration_sec"] = 0.0
    now_iso = datetime.now(timezone.utc).isoformat()
    sequence["created_at"] = sequence.get("created_at") or now_iso
    sequence["updated_at"] = now_iso
    store = upsert_sequence_in_store(store, sequence)
    save_vapor_sequence_store(store)
    with vapor_sequence_lock:
        vapor_sequence_runtime["recording"] = True
        vapor_sequence_runtime["record_started_monotonic"] = time.monotonic()
        vapor_sequence_runtime["record_sequence_id"] = sequence["id"]
    return vapor_sequence_status(sequence["id"])


def stop_vapor_sequence_recording():
    with vapor_sequence_lock:
        vapor_sequence_runtime["recording"] = False
        vapor_sequence_runtime["record_started_monotonic"] = None
        vapor_sequence_runtime["record_sequence_id"] = None
    return vapor_sequence_status()


def clear_vapor_sequence(sequence_id=None):
    stop_vapor_sequence_playback()
    with vapor_sequence_lock:
        vapor_sequence_runtime["recording"] = False
        vapor_sequence_runtime["record_started_monotonic"] = None
        vapor_sequence_runtime["record_sequence_id"] = None
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    cleared = default_vapor_sequence_item(sequence_id=sequence.get("id"), name=sequence.get("name"))
    store = upsert_sequence_in_store(store, cleared)
    save_vapor_sequence_store(store)
    return vapor_sequence_status(sequence.get("id"))


def append_vapor_sequence_step(actuator, enabled):
    with vapor_sequence_lock:
        if not vapor_sequence_runtime.get("recording"):
            return None
        started = vapor_sequence_runtime.get("record_started_monotonic")
        sequence_id = vapor_sequence_runtime.get("record_sequence_id")
    if not started:
        return None
    elapsed_ms = int(round((time.monotonic() - started) * 1000.0))
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    steps = list(sequence.get("steps", []))
    step = {
        "actuator": str(actuator).strip().lower(),
        "enabled": bool(enabled),
        "at_ms": max(0, elapsed_ms),
    }
    if steps:
        last = steps[-1]
        if last.get("actuator") == step["actuator"] and bool(last.get("enabled")) == step["enabled"]:
            return sequence
    steps.append(step)
    sequence["steps"] = steps
    sequence["updated_at"] = datetime.now(timezone.utc).isoformat()
    store = upsert_sequence_in_store(store, sequence)
    save_vapor_sequence_store(store)
    return sequence


def set_actuator_enabled(actuator, enabled):
    next_enabled = "true" if enabled else "false"
    if actuator == "vapor":
        return vapor_request("POST", "/api/vapor/set", {"enabled": next_enabled})
    if actuator == "fan":
        return fan_request("POST", "/api/fan/set", {"enabled": next_enabled})
    if actuator == "smoke":
        return smoke_request("POST", "/api/smoke/set", {"enabled": next_enabled})
    raise ValueError("invalid_actuator")


def run_vapor_sequence_playback(sequence):
    stop_event = None
    with vapor_sequence_lock:
        stop_event = vapor_sequence_runtime.get("play_stop_event")
    try:
        initial = sequence.get("initial", {})
        set_actuator_enabled("vapor", bool(initial.get("vapor", False)))
        set_actuator_enabled("fan", bool(initial.get("fan", False)))
        set_actuator_enabled("smoke", bool(initial.get("smoke", False)))
        steps = list(sequence.get("steps", []))
        started = time.monotonic()
        for step in steps:
            target_delay = max(0.0, float(step.get("at_ms", 0)) / 1000.0)
            while True:
                if stop_event and stop_event.wait(0.02):
                    return
                remaining = target_delay - (time.monotonic() - started)
                if remaining <= 0:
                    break
                if stop_event and stop_event.wait(min(0.1, remaining)):
                    return
            set_actuator_enabled(step.get("actuator"), bool(step.get("enabled", False)))
        sequence["last_played_at"] = datetime.now(timezone.utc).isoformat()
        store = load_vapor_sequence_store()
        store = upsert_sequence_in_store(store, sequence)
        save_vapor_sequence_store(store)
    finally:
        with vapor_sequence_lock:
            vapor_sequence_runtime["playing"] = False
            vapor_sequence_runtime["play_started_monotonic"] = None
            vapor_sequence_runtime["play_sequence_id"] = None
            vapor_sequence_runtime["play_thread"] = None
            vapor_sequence_runtime["play_stop_event"] = threading.Event()


def start_vapor_sequence_playback(sequence_id=None):
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    if not sequence.get("steps"):
        raise ValueError("sequence_empty")
    with vapor_sequence_lock:
        if vapor_sequence_runtime.get("recording"):
            raise ValueError("sequence_recording")
    stop_vapor_sequence_playback()
    with vapor_sequence_lock:
        vapor_sequence_runtime["playing"] = True
        vapor_sequence_runtime["play_started_monotonic"] = time.monotonic()
        vapor_sequence_runtime["play_sequence_id"] = sequence.get("id")
        vapor_sequence_runtime["play_stop_event"] = threading.Event()
        thread = threading.Thread(
            target=run_vapor_sequence_playback,
            args=(sequence,),
            daemon=True,
        )
        vapor_sequence_runtime["play_thread"] = thread
        thread.start()
    return vapor_sequence_status(sequence.get("id"))


def create_vapor_sequence(name):
    store = load_vapor_sequence_store()
    existing_names = {str(item.get("name") or "").strip().lower() for item in store.get("sequences", [])}
    base_name = str(name or "").strip() or f"Secuencia {len(store.get('sequences', [])) + 1}"
    candidate_name = base_name
    suffix = 2
    while candidate_name.strip().lower() in existing_names:
        candidate_name = f"{base_name} {suffix}"
        suffix += 1
    sequence = default_vapor_sequence_item(sequence_id=uuid.uuid4().hex, name=candidate_name)
    sequence["created_at"] = datetime.now(timezone.utc).isoformat()
    sequence["updated_at"] = sequence["created_at"]
    store = upsert_sequence_in_store(store, sequence)
    save_vapor_sequence_store(store)
    return vapor_sequence_status(sequence["id"])


def rename_vapor_sequence(sequence_id, name):
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    sequence["name"] = str(name or "").strip() or sequence.get("name") or "Secuencia"
    sequence["updated_at"] = datetime.now(timezone.utc).isoformat()
    store = upsert_sequence_in_store(store, sequence)
    save_vapor_sequence_store(store)
    return vapor_sequence_status(sequence["id"])


def delete_vapor_sequence(sequence_id):
    store = load_vapor_sequence_store()
    sequences = [item for item in store.get("sequences", []) if item.get("id") != sequence_id]
    if not sequences:
        sequences = default_vapor_sequence_store()["sequences"]
    store["sequences"] = sequences
    if store.get("active_sequence_id") == sequence_id:
        store["active_sequence_id"] = sequences[0]["id"]
    save_vapor_sequence_store(store)
    return vapor_sequence_status(store["active_sequence_id"])


def select_vapor_sequence(sequence_id):
    store = load_vapor_sequence_store()
    sequence = get_sequence_from_store(store, sequence_id)
    store["active_sequence_id"] = sequence["id"]
    save_vapor_sequence_store(store)
    return vapor_sequence_status(sequence["id"])


def default_vapor_automation_config():
    return {
        "rules": [],
    }


def normalize_vapor_automation_rule(raw):
    raw = raw or {}
    rid = raw.get("id") or uuid.uuid4().hex
    return {
        "id": rid,
        "name": str(raw.get("name") or rid),
        "device_id": raw.get("device_id") or "",
        "metric_id": raw.get("metric_id") or "",
        "sequence_id": str(raw.get("sequence_id") or "").strip() or None,
        "min_value": raw.get("min_value"),
        "max_value": raw.get("max_value"),
        "min_delta": float(raw.get("min_delta") or 0.0),
        "cooldown_sec": float(raw.get("cooldown_sec") or 10.0),
        "enabled": bool(raw.get("enabled", True)),
        "muted": bool(raw.get("muted", False)),
    }


def load_vapor_automation_config():
    if not os.path.exists(VAPOR_AUTOMATION_CONFIG_PATH):
        return default_vapor_automation_config()
    try:
        with open(VAPOR_AUTOMATION_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_vapor_automation_config()
        data.setdefault("rules", [])
        data["rules"] = [normalize_vapor_automation_rule(rule) for rule in (data.get("rules") or []) if isinstance(rule, dict)]
        return data
    except Exception:
        return default_vapor_automation_config()


def save_vapor_automation_config(config):
    payload = default_vapor_automation_config()
    payload.update(config or {})
    payload["rules"] = [normalize_vapor_automation_rule(rule) for rule in (payload.get("rules") or []) if isinstance(rule, dict)]
    os.makedirs(os.path.dirname(VAPOR_AUTOMATION_CONFIG_PATH), exist_ok=True)
    with open(VAPOR_AUTOMATION_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


class VaporAutomationEngine:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.rule_state = {}
        self.active_rule_ids = set()
        self.triggering_rule_id = None
        self.triggering_sequence_id = None

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def status(self):
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "active_rule_ids": sorted(self.active_rule_ids),
            "triggering_rule_id": self.triggering_rule_id,
            "triggering_sequence_id": self.triggering_sequence_id,
            "rule_runtime": self.runtime_status(),
        }

    def runtime_status(self):
        now = time.time()
        runtime = {}
        sequence_runtime = vapor_sequence_status(self.triggering_sequence_id)
        for rule_id, state in self.rule_state.items():
            rule_id = str(rule_id)
            cooldown_sec = float(state.get("cooldown_sec") or 0.0)
            last_trigger_ts = float(state.get("last_trigger_ts") or 0.0)
            cooldown_remaining_sec = 0.0
            if cooldown_sec > 0 and last_trigger_ts > 0:
                cooldown_remaining_sec = round(max(0.0, cooldown_sec - (now - last_trigger_ts)), 1)
            playback_progress_pct = 0.0
            play_elapsed_sec = 0.0
            play_duration_sec = 0.0
            playing = bool(sequence_runtime.get("playing")) and str(self.triggering_rule_id or "") == rule_id
            if playing:
                play_elapsed_sec = float(sequence_runtime.get("play_elapsed_sec") or 0.0)
                play_duration_sec = float(sequence_runtime.get("duration_sec") or 0.0)
                if play_duration_sec > 0:
                    playback_progress_pct = round(max(0.0, min(100.0, (play_elapsed_sec / play_duration_sec) * 100.0)), 1)
            runtime[rule_id] = {
                "in_range": bool(state.get("in_range", False)),
                "muted": bool(state.get("muted", False)),
                "playing": playing,
                "play_elapsed_sec": round(play_elapsed_sec, 2),
                "play_duration_sec": round(play_duration_sec, 2),
                "playback_progress_pct": playback_progress_pct,
                "cooldown_remaining_sec": cooldown_remaining_sec,
                "last_trigger_ts": last_trigger_ts,
            }
        return runtime

    def run(self):
        while not self.stop_event.wait(1.0):
            config = load_vapor_automation_config()
            latest = get_latest_map()
            next_active = set()
            rules = config.get("rules", [])
            for rule in rules:
                if not rule.get("enabled", True):
                    self.rule_state.pop(rule.get("id"), None)
                    continue
                metric_id = rule.get("metric_id")
                device_id = rule.get("device_id")
                if not metric_id:
                    continue
                item = get_metric(latest, metric_id, device_id)
                if not item:
                    continue
                try:
                    value = float(item["value"])
                except Exception:
                    continue
                state = self.rule_state.setdefault(rule["id"], {
                    "last_value": None,
                    "last_trigger_ts": 0.0,
                    "active": False,
                    "cooldown_sec": float(rule.get("cooldown_sec") or 10.0),
                    "in_range": False,
                    "muted": False,
                })
                min_value = rule.get("min_value")
                max_value = rule.get("max_value")
                min_delta = float(rule.get("min_delta") or 0.0)
                cooldown = float(rule.get("cooldown_sec") or 10.0)
                muted = bool(rule.get("muted", False))
                in_range = True
                if min_value not in (None, ""):
                    in_range = in_range and value >= float(min_value)
                if max_value not in (None, ""):
                    in_range = in_range and value <= float(max_value)
                delta = None if state["last_value"] is None else abs(value - state["last_value"])
                changed = state["last_value"] is None or delta >= min_delta
                now = time.time()
                state["cooldown_sec"] = cooldown
                state["in_range"] = in_range
                state["muted"] = muted
                if in_range:
                    next_active.add(str(rule["id"]))
                should_trigger = (
                    in_range
                    and not muted
                    and changed
                    and (now - state["last_trigger_ts"] >= cooldown)
                )
                if should_trigger:
                    try:
                        sequence_id = str(rule.get("sequence_id") or "").strip() or None
                        runtime = vapor_sequence_status(sequence_id)
                        if not runtime.get("recording") and not runtime.get("playing"):
                            self.triggering_rule_id = str(rule["id"])
                            self.triggering_sequence_id = sequence_id or runtime.get("sequence_id")
                            start_vapor_sequence_playback(sequence_id)
                            state["last_trigger_ts"] = now
                            state["active"] = True
                    except Exception:
                        pass
                elif not in_range:
                    state["active"] = False
                elif now - state["last_trigger_ts"] >= cooldown:
                    state["active"] = False
                state["last_value"] = value
            self.active_rule_ids = next_active
            runtime = vapor_sequence_status(self.triggering_sequence_id)
            if not runtime.get("playing"):
                self.triggering_rule_id = None
                self.triggering_sequence_id = None


vapor_automation_engine = VaporAutomationEngine()



os.makedirs(SOUND_DIR, exist_ok=True)


def default_sound_config():
    return {
        "enabled": False,
        "output_device": DEFAULT_USB_AUDIO_DEVICE,
        "rules": [],
    }


def normalize_sound_rule(raw):
    raw = raw or {}
    rid = raw.get("id") or uuid.uuid4().hex
    raw_stop_rule_ids = raw.get("stop_rule_ids")
    stop_rule_ids = []
    if isinstance(raw_stop_rule_ids, list):
        for value in raw_stop_rule_ids:
            candidate = str(value or "").strip()
            if candidate and candidate != rid and candidate not in stop_rule_ids:
                stop_rule_ids.append(candidate)
    legacy_stop_rule_id = str(raw.get("stop_rule_id") or "").strip() or None
    if legacy_stop_rule_id and legacy_stop_rule_id != rid and legacy_stop_rule_id not in stop_rule_ids:
        stop_rule_ids.append(legacy_stop_rule_id)
    stop_rule_ids = stop_rule_ids[:2]
    stop_rule_id = stop_rule_ids[0] if stop_rule_ids else None
    mode = str(raw.get("mode") or "once").strip().lower()
    if mode not in {"once", "loop", "interval"}:
        mode = "once"
    volume_pct = max(0, min(100, int(round(float(raw.get("volume_pct") or 100)))))
    return {
        "id": rid,
        "name": str(raw.get("name") or rid),
        "device_id": raw.get("device_id") or "",
        "metric_id": raw.get("metric_id") or "",
        "sound_file": raw.get("sound_file") or "",
        "mode": mode,
        "min_value": raw.get("min_value"),
        "max_value": raw.get("max_value"),
        "min_delta": float(raw.get("min_delta") or 0.0),
        "cooldown_sec": float(raw.get("cooldown_sec") or 10.0),
        "enabled": bool(raw.get("enabled", True)),
        "muted": bool(raw.get("muted", False)),
        "volume_pct": volume_pct,
        "stop_rule_ids": stop_rule_ids,
        "stop_rule_id": stop_rule_id,
    }


def load_sound_config():
    if not os.path.exists(SOUND_CONFIG_PATH):
        return default_sound_config()
    try:
        with open(SOUND_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_sound_config()
        data.setdefault("enabled", False)
        data.setdefault("output_device", DEFAULT_USB_AUDIO_DEVICE)
        data.setdefault("rules", [])
        data["rules"] = [normalize_sound_rule(rule) for rule in (data.get("rules") or []) if isinstance(rule, dict)]
        return data
    except Exception:
        return default_sound_config()


def save_sound_config(config):
    payload = default_sound_config()
    payload.update(config or {})
    payload["rules"] = [normalize_sound_rule(rule) for rule in (payload.get("rules") or []) if isinstance(rule, dict)]
    with open(SOUND_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def configured_output_device():
    config = load_sound_config()
    return str(config.get("output_device") or "").strip()


def current_sound_card_name(output_device=None):
    device = str(output_device or configured_output_device() or "").strip()
    if "Headphones" in device:
        return "Headphones"
    if "CD002" in device:
        return "CD002"
    return None


def get_sound_volume(output_device=None):
    card_name = current_sound_card_name(output_device)
    if not card_name:
        return {"card": None, "percent": None, "control": "PCM"}
    try:
        result = subprocess.run(
            ["amixer", "-c", card_name, "sget", "PCM"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return {"card": card_name, "percent": None, "control": "PCM"}
        match = re.search(r"\[(\d{1,3})%\]", result.stdout)
        percent = int(match.group(1)) if match else None
        return {"card": card_name, "percent": percent, "control": "PCM"}
    except Exception:
        return {"card": card_name, "percent": None, "control": "PCM"}


def set_sound_volume(percent, output_device=None):
    card_name = current_sound_card_name(output_device)
    if not card_name:
        raise ValueError("sound_output_unavailable")
    value = max(0, min(100, int(round(float(percent)))))
    result = subprocess.run(
        ["amixer", "-c", card_name, "sset", "PCM", f"{value}%"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "amixer_set_failed")
    return get_sound_volume(output_device)


FX_COLOR_PRESETS = {
    "auto": None,
    "white": [255, 255, 255],
    "green": [46, 204, 113],
    "yellow": [241, 196, 15],
    "orange": [243, 156, 18],
    "red": [231, 76, 60],
    "blue": [52, 152, 219],
    "purple": [155, 89, 182],
}

FX_EFFECT_PRESETS = {
    "auto": {
        "label": "Auto",
    },
    "none": {
        "label": "Sin efecto",
    },
    "rain": {
        "label": "Lluvia",
    },
    "dust": {
        "label": "Polvo",
    },
    "lightning": {
        "label": "Rayo",
    },
}


def default_fx_config():
    return {
        "text_color_mode": "auto",
        "effect_mode": "auto",
        "brightness": 24,
    }


def load_fx_config():
    if not os.path.exists(FX_CONFIG_PATH):
        return default_fx_config()
    try:
        with open(FX_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_fx_config()
        mode = str(data.get("text_color_mode") or "auto").lower()
        if mode not in FX_COLOR_PRESETS:
            mode = "auto"
        effect_mode = str(data.get("effect_mode") or "none").lower()
        if effect_mode not in FX_EFFECT_PRESETS:
            effect_mode = "auto"
        try:
            brightness = int(round(float(data.get("brightness", 24))))
        except Exception:
            brightness = 24
        if brightness < 0:
            brightness = 0
        if brightness > 40:
            brightness = 40
        return {
            "text_color_mode": mode,
            "effect_mode": effect_mode,
            "brightness": brightness,
        }
    except Exception:
        return default_fx_config()


def save_fx_config(config):
    os.makedirs(os.path.dirname(FX_CONFIG_PATH), exist_ok=True)
    with open(FX_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def normalize_degrees(value):
    try:
        deg = float(value)
    except (TypeError, ValueError):
        return None
    deg = deg % 360.0
    if deg < 0:
        deg += 360.0
    return deg


def default_wind_calibration():
    return {
        "offset_deg": 0.0,
        "east_reference_deg": None,
        "updated_at": None,
    }


def load_wind_calibration():
    if not os.path.exists(WIND_CALIBRATION_PATH):
        return default_wind_calibration()
    try:
        with open(WIND_CALIBRATION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_wind_calibration()
        calibration = default_wind_calibration()
        calibration["offset_deg"] = normalize_degrees(data.get("offset_deg")) or 0.0
        calibration["east_reference_deg"] = normalize_degrees(data.get("east_reference_deg"))
        calibration["updated_at"] = data.get("updated_at")
        return calibration
    except Exception:
        return default_wind_calibration()


def save_wind_calibration(config):
    os.makedirs(os.path.dirname(WIND_CALIBRATION_PATH), exist_ok=True)
    calibration = default_wind_calibration()
    calibration["offset_deg"] = normalize_degrees(config.get("offset_deg")) or 0.0
    calibration["east_reference_deg"] = normalize_degrees(config.get("east_reference_deg"))
    calibration["updated_at"] = config.get("updated_at")
    with open(WIND_CALIBRATION_PATH, "w", encoding="utf-8") as f:
        json.dump(calibration, f, ensure_ascii=False, indent=2)
    return calibration


def apply_wind_calibration(raw_deg, calibration=None):
    deg = normalize_degrees(raw_deg)
    if deg is None:
        return None
    calibration = calibration or load_wind_calibration()
    offset = normalize_degrees(calibration.get("offset_deg")) or 0.0
    return normalize_degrees(deg + offset)


def current_wind_calibration_state():
    calibration = load_wind_calibration()
    latest = get_latest_map()
    wind_dir = get_metric(latest, "wind_direction_deg", "wind_esp8266")
    raw_deg = metric_float(wind_dir)
    corrected_deg = apply_wind_calibration(raw_deg, calibration)
    return {
        "ok": True,
        "calibration": calibration,
        "raw_direction_deg": raw_deg,
        "calibrated_direction_deg": corrected_deg,
        "ts": wind_dir.get("ts") if wind_dir else None,
    }


def get_latest_interpretation_history():
    rows = get_recent_interpretation_history(limit=1, seconds=72 * 3600)
    return rows[0] if rows else None


def derive_fx_hint(interp=None):
    interp = interp or {}
    state = interp.get("state") or {}
    forecast = interp.get("forecast") or {}
    air = interp.get("air") or {}
    state_id = str(state.get("id") or "").strip()
    forecast_label = str(forecast.get("label") or "").strip().lower()
    air_band = str(air.get("band") or "").strip()

    rain_states = {
        "lluvia_fuerte",
        "dia_lluvioso",
        "lluvia_continua",
        "lluvia_suave",
        "llovizna_fina",
        "lluvia_y_frio",
        "lluvia_beneficiosa",
        "lluvia_suave_y_humedad_alta",
        "sol_despues_de_lluvia",
    }
    air_states = {
        "aire_limpio",
        "aire_regular_o_pm_alta",
        "aire_malo",
        "pm_alta_y_poco_viento",
        "pm_alta_y_trafico",
        "pm10_alta_o_polvo",
        "pm25_alta",
        "humo_o_combustion",
        "particulas_subiendo",
        "particulas_bajando",
    }
    storm_states = {
        "presion_bajando_rapido",
        "nubosidad_creciente",
        "inestabilidad_probable",
        "presion_baja_y_humedad_alta",
        "nubes_densas_y_presion_baja",
    }
    calm_states = {
        "presion_estable",
        "aire_limpio",
        "estable",
        "mejora",
        "dia_luminoso",
        "sol_suave",
        "sol_fuerte_o_calor",
        "alta_presion_y_sol",
    }
    moist_states = {
        "humedad_alta",
        "humedad_muy_alta_sin_lluvia",
        "niebla_y_humedad_alta",
        "niebla_densa_o_dia_gris",
        "cielo_gris_estable",
        "rocio",
        "condensacion",
    }

    if state_id in rain_states or "lluvia" in forecast_label:
        return {
            "color_mode": "blue",
            "effect_mode": "rain",
            "reason": "lluvia o precipitación detectada",
        }
    if state_id in storm_states or "inestabilidad" in forecast_label:
        return {
            "color_mode": "orange",
            "effect_mode": "lightning",
            "reason": "presión cayendo o inestabilidad clara",
        }
    if state_id in air_states:
        if state_id == "aire_limpio":
            return {
                "color_mode": "green",
                "effect_mode": "none",
                "reason": "aire limpio interpretado por la estación",
            }
        if air_band == "muy_mala" or state_id in {"aire_malo", "humo_o_combustion", "pm_alta_y_poco_viento", "pm25_alta", "pm10_alta_o_polvo"}:
            return {
                "color_mode": "red",
                "effect_mode": "dust",
                "reason": "aire cargado o partículas altas",
            }
        if air_band == "mala":
            return {
                "color_mode": "orange",
                "effect_mode": "dust",
                "reason": "aire cargado",
            }
        return {
            "color_mode": "yellow",
            "effect_mode": "dust",
            "reason": "calidad de aire regular",
        }
    if state_id in moist_states or "niebla" in forecast_label:
        return {
            "color_mode": "white",
            "effect_mode": "none",
            "reason": "humedad alta, niebla o cielo cerrado",
        }
    if state_id in calm_states:
        return {
            "color_mode": "green",
            "effect_mode": "none",
            "reason": "situación estable o de mejora",
        }

    return {
        "color_mode": "blue",
        "effect_mode": "none",
        "reason": "sin señal dominante para FX",
    }


def resolve_fx_display(fx_config, fx_hint):
    fx_config = fx_config or default_fx_config()
    fx_hint = fx_hint or {}
    text_color_mode = fx_config.get("text_color_mode", "auto")
    effect_mode = fx_config.get("effect_mode", "auto")
    color_mode = text_color_mode if text_color_mode != "auto" else fx_hint.get("color_mode", "auto")
    applied_effect = effect_mode if effect_mode != "auto" else fx_hint.get("effect_mode", "none")
    return {
        "text_color_mode": text_color_mode,
        "effect_mode": effect_mode,
        "color_mode": color_mode,
        "effect": applied_effect,
        "reason": fx_hint.get("reason", ""),
    }


def list_sound_files():
    os.makedirs(SOUND_DIR, exist_ok=True)
    items = []
    for name in sorted(os.listdir(SOUND_DIR)):
        path = os.path.join(SOUND_DIR, name)
        if os.path.isfile(path):
            items.append({
                "name": name,
                "size": os.path.getsize(path),
            })
    return items


def build_chirp_file(path, base_hz=1200.0, span_hz=900.0, duration=0.9):
    sample_rate = 22050
    total = int(sample_rate * duration)
    frames = bytearray()
    for i in range(total):
        t = i / sample_rate
        env = math.sin(math.pi * min(1.0, t / duration)) ** 2
        chirp = math.sin(2 * math.pi * (base_hz + span_hz * math.sin(2 * math.pi * 3.5 * t)) * t)
        overtone = math.sin(2 * math.pi * (base_hz * 1.8 + span_hz * 0.15 * math.cos(2 * math.pi * 5.2 * t)) * t)
        sample = (0.42 * chirp + 0.18 * overtone) * env
        pcm = int(max(-1.0, min(1.0, sample)) * 32767)
        frames.extend(struct.pack("<h", pcm))
    with wave.open(path, "wb") as wavf:
        wavf.setnchannels(1)
        wavf.setsampwidth(2)
        wavf.setframerate(sample_rate)
        wavf.writeframes(bytes(frames))


def build_chirp_pcm(base_hz=1200.0, span_hz=900.0, duration=0.9):
    total = int(22050 * duration)
    samples = array("h")
    for i in range(total):
        t = i / 22050
        env = math.sin(math.pi * min(1.0, t / duration)) ** 2
        chirp = math.sin(2 * math.pi * (base_hz + span_hz * math.sin(2 * math.pi * 3.5 * t)) * t)
        overtone = math.sin(2 * math.pi * (base_hz * 1.8 + span_hz * 0.15 * math.cos(2 * math.pi * 5.2 * t)) * t)
        sample = (0.42 * chirp + 0.18 * overtone) * env
        pcm = int(max(-1.0, min(1.0, sample)) * 32767)
        samples.append(pcm)
        samples.append(pcm)
    return samples

class SoundEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.audio_thread = None
        self.player = None
        self.player_device = None
        self.rule_state = {}
        self.global_enabled = False
        self.sample_rate = 22050
        self.channels = 2
        self.chunk_frames = 2048
        self.voices = []
        self.suppressed_rule_ids = set()
        self.preview_rule_until = {}
        self.preview_rule_targets = {}
        self.decoded_cache = {}
        self.last_audio_error = None

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
        if not self.audio_thread or not self.audio_thread.is_alive():
            self.audio_thread = threading.Thread(target=self.audio_loop, daemon=True)
            self.audio_thread.start()

    def stop(self):
        self.stop_event.set()
        self.stop_sound()
        with self.lock:
            self._stop_player_locked()

    def status(self):
        with self.lock:
            names = [voice["name"] for voice in self.voices]
            active_rule_ids = sorted({
                str(voice.get("rule_id") or "")
                for voice in self.voices
                if voice.get("rule_id")
            })
            return {
                "enabled": self.global_enabled,
                "playing": bool(self.voices),
                "current_rule_id": ",".join(active_rule_ids) or None,
                "active_rule_ids": active_rule_ids,
                "suppressed_rule_ids": sorted(self.suppressed_rule_ids),
                "preview_rule_ids": sorted([
                    rule_id for rule_id, until_ts in self.preview_rule_until.items()
                    if until_ts > time.time()
                ]),
                "current_name": ", ".join(names) if names else None,
                "current_loop": any(voice.get("loop") for voice in self.voices),
                "voice_count": len(self.voices),
            }

    def runtime_status(self, config_rules=None):
        now = time.time()
        config_rules = config_rules or []
        with self.lock:
            voices = [dict(voice) for voice in self.voices]
            rule_state = {str(rule_id): dict(state or {}) for rule_id, state in self.rule_state.items()}
            suppressed_rule_ids = set(self.suppressed_rule_ids)
            preview_rule_until = dict(self.preview_rule_until)
        runtime = {}
        voices_by_rule = {}
        for voice in voices:
            rule_id = str(voice.get("rule_id") or "").strip()
            if not rule_id:
                continue
            voices_by_rule.setdefault(rule_id, []).append(voice)
        for rule in config_rules:
            rule_id = str(rule.get("id") or "").strip()
            if not rule_id:
                continue
            entry = {
                "playing": False,
                "suppressed": rule_id in suppressed_rule_ids,
                "preview": preview_rule_until.get(rule_id, 0) > now,
                "playback_elapsed_sec": 0.0,
                "playback_duration_sec": 0.0,
                "playback_progress_pct": 0.0,
                "cooldown_remaining_sec": 0.0,
            }
            matching_voices = voices_by_rule.get(rule_id) or []
            if matching_voices:
                voice = matching_voices[0]
                duration_sec = float(voice.get("duration_sec") or 0.0)
                elapsed_sec = 0.0
                if self.channels and self.sample_rate:
                    elapsed_sec = float(voice.get("position", 0)) / float(self.channels * self.sample_rate)
                progress_pct = 0.0
                if duration_sec > 0:
                    progress_pct = max(0.0, min(100.0, (elapsed_sec / duration_sec) * 100.0))
                entry.update({
                    "playing": True,
                    "playback_elapsed_sec": round(elapsed_sec, 2),
                    "playback_duration_sec": round(duration_sec, 2),
                    "playback_progress_pct": round(progress_pct, 1),
                })
            state = rule_state.get(rule_id) or {}
            cooldown_sec = float(rule.get("cooldown_sec") or 0.0)
            last_trigger_ts = float(state.get("last_trigger_ts") or 0.0)
            if cooldown_sec > 0 and last_trigger_ts > 0:
                entry["cooldown_remaining_sec"] = round(max(0.0, cooldown_sec - (now - last_trigger_ts)), 1)
            runtime[rule_id] = entry
        return runtime

    def set_enabled(self, enabled):
        with self.lock:
            self.global_enabled = bool(enabled)
            if not self.global_enabled:
                self.voices.clear()
                self.suppressed_rule_ids.clear()
                self.preview_rule_until.clear()
                self.preview_rule_targets.clear()
                self._stop_player_locked()

    def _stop_player_locked(self):
        if self.player:
            try:
                if self.player.stdin:
                    self.player.stdin.close()
            except Exception:
                pass
            try:
                self.player.terminate()
                self.player.wait(timeout=2)
            except Exception:
                try:
                    self.player.kill()
                except Exception:
                    pass
        self.player = None
        self.player_device = None

    def _ensure_player_locked(self):
        if self.player and self.player.poll() is None:
            return True
        self._stop_player_locked()
        device = resolve_audio_device()
        if not device:
            self.last_audio_error = "No audio output device available"
            return False
        try:
            self.player = subprocess.Popen(
                [
                    "aplay",
                    "-q",
                    "-D",
                    device,
                    "-f",
                    "S16_LE",
                    "-c",
                    str(self.channels),
                    "-r",
                    str(self.sample_rate),
                    "-t",
                    "raw",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.player_device = device
            self.last_audio_error = None
            return True
        except Exception as exc:
            self.last_audio_error = f"Failed to start audio player on {device}: {exc}"
            self.player = None
            self.player_device = None
            return False

    def decode_file(self, path):
        cached = self.decoded_cache.get(path)
        if cached is not None:
            return cached
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        proc = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "quiet",
                "-i",
                path,
                "-f",
                "s16le",
                "-ac",
                str(self.channels),
                "-ar",
                str(self.sample_rate),
                "-",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        samples = array("h")
        samples.frombytes(proc.stdout)
        self.decoded_cache[path] = samples
        return samples

    def is_rule_playing(self, rule_id):
        return any(voice.get("rule_id") == rule_id for voice in self.voices)

    def stop_rule_sound(self, rule_id):
        with self.lock:
            self.voices = [voice for voice in self.voices if voice.get("rule_id") != rule_id]

    def set_rule_gain(self, rule_id, gain):
        safe_gain = max(0.0, min(1.0, float(gain or 0.0)))
        with self.lock:
            for voice in self.voices:
                if voice.get("rule_id") == rule_id:
                    voice["gain"] = safe_gain

    def stop_rule_targets(self, rule_ids):
        targets = {str(rule_id or "").strip() for rule_id in (rule_ids or []) if str(rule_id or "").strip()}
        if not targets:
            return
        with self.lock:
            self.voices = [voice for voice in self.voices if str(voice.get("rule_id") or "") not in targets]

    def stop_sound(self):
        with self.lock:
            self.voices.clear()
            self.suppressed_rule_ids.clear()
            self.preview_rule_until.clear()
            self.preview_rule_targets.clear()

    def _queue_voice_locked(self, samples, loop, name, rule_id, gain=1.0):
        duration_sec = 0.0
        if self.channels and self.sample_rate:
            duration_sec = len(samples) / float(self.channels * self.sample_rate)
        self.voices.append({
            "samples": samples,
            "position": 0,
            "loop": loop,
            "name": name,
            "rule_id": rule_id,
            "gain": max(0.0, min(1.0, float(gain or 0.0))),
            "duration_sec": duration_sec,
        })

    def play_file(self, filename, loop=False, rule_id=None, gain=1.0):
        if filename == "__chirp__":
            self.play_chirp(rule_id=rule_id, gain=gain)
            return
        path = os.path.join(SOUND_DIR, filename)
        samples = self.decode_file(path)
        with self.lock:
            self._queue_voice_locked(samples, loop=loop, name=filename, rule_id=rule_id, gain=gain)

    def play_chirp(self, rule_id="test", gain=1.0):
        samples = build_chirp_pcm()
        with self.lock:
            self._queue_voice_locked(samples, loop=False, name="chirp", rule_id=rule_id, gain=gain)

    def start_rule_preview(self, rule_id, filename, gain=1.0, stop_rule_ids=None):
        stop_targets = [
            str(value or "").strip()
            for value in (stop_rule_ids or [])
            if str(value or "").strip()
        ]
        duration_sec = 1.0
        if filename == "__chirp__":
            samples = build_chirp_pcm()
            duration_sec = len(samples) / float(self.sample_rate * self.channels)
            with self.lock:
                self.voices = [voice for voice in self.voices if voice.get("rule_id") != rule_id]
                self._queue_voice_locked(samples, loop=False, name="chirp", rule_id=rule_id, gain=gain)
                self.preview_rule_until[str(rule_id)] = time.time() + duration_sec + 0.5
                self.preview_rule_targets[str(rule_id)] = stop_targets
        else:
            path = os.path.join(SOUND_DIR, filename)
            samples = self.decode_file(path)
            duration_sec = len(samples) / float(self.sample_rate * self.channels)
            with self.lock:
                self.voices = [voice for voice in self.voices if voice.get("rule_id") != rule_id]
                self._queue_voice_locked(samples, loop=False, name=filename, rule_id=rule_id, gain=gain)
                self.preview_rule_until[str(rule_id)] = time.time() + duration_sec + 0.5
                self.preview_rule_targets[str(rule_id)] = stop_targets
        if stop_targets:
            self.stop_rule_targets(stop_targets)

    def audio_loop(self):
        while not self.stop_event.wait(0.01):
            player = None
            payload = None
            with self.lock:
                if not self.global_enabled:
                    self._stop_player_locked()
                    continue
                if not self.voices:
                    self._stop_player_locked()
                    continue

                if not self._ensure_player_locked():
                    continue
                mix = [0] * (self.chunk_frames * self.channels)
                next_voices = []
                for voice in self.voices:
                    samples = voice["samples"]
                    pos = voice["position"]
                    gain = max(0.0, min(1.0, float(voice.get("gain", 1.0) or 0.0)))
                    for frame in range(self.chunk_frames):
                        if pos >= len(samples):
                            if voice["loop"]:
                                pos = 0
                            else:
                                break
                        for ch in range(self.channels):
                            if pos + ch < len(samples):
                                mix[frame * self.channels + ch] += int(samples[pos + ch] * gain)
                        pos += self.channels
                    if pos < len(samples) or voice["loop"]:
                        voice["position"] = pos
                        next_voices.append(voice)
                self.voices = next_voices

                out = array("h")
                for value in mix:
                    if value > 32767:
                        value = 32767
                    elif value < -32768:
                        value = -32768
                    out.append(int(value))
                if self.player and self.player.stdin:
                    player = self.player
                    payload = out.tobytes()
            if player and payload:
                try:
                    player.stdin.write(payload)
                    player.stdin.flush()
                except Exception:
                    with self.lock:
                        if self.player is player:
                            self._stop_player_locked()

    def run(self):
        while not self.stop_event.wait(1.0):
            config = load_sound_config()
            self.set_enabled(config.get("enabled", False))
            if not self.global_enabled:
                continue
            now = time.time()
            with self.lock:
                expired_previews = [
                    rule_id for rule_id, until_ts in self.preview_rule_until.items()
                    if until_ts <= now
                ]
                for rule_id in expired_previews:
                    self.preview_rule_until.pop(rule_id, None)
                    self.preview_rule_targets.pop(rule_id, None)
            latest = get_latest_map()
            rules = config.get("rules", [])
            rule_contexts = []
            for rule in rules:
                if not rule.get("enabled", True):
                    continue
                metric_id = rule.get("metric_id")
                device_id = rule.get("device_id")
                sound_file = rule.get("sound_file")
                if not metric_id or not sound_file:
                    continue
                muted = bool(rule.get("muted", False))
                stop_rule_ids = [
                    str(value or "").strip()
                    for value in (rule.get("stop_rule_ids") or [])
                    if str(value or "").strip()
                ]
                if not stop_rule_ids:
                    legacy_stop_rule_id = str(rule.get("stop_rule_id") or "").strip() or None
                    if legacy_stop_rule_id:
                        stop_rule_ids = [legacy_stop_rule_id]
                item = get_metric(latest, metric_id, device_id)
                if not item:
                    continue
                value = float(item["value"])
                min_value = rule.get("min_value")
                max_value = rule.get("max_value")
                in_range = True
                if min_value not in (None, ""):
                    in_range = in_range and value >= float(min_value)
                if max_value not in (None, ""):
                    in_range = in_range and value <= float(max_value)
                context = {
                    "rule": rule,
                    "value": value,
                    "muted": muted,
                    "stop_rule_ids": stop_rule_ids,
                    "in_range": in_range,
                }
                rule_contexts.append(context)
            suppressed_rule_ids = set()
            with self.lock:
                preview_rule_until = dict(self.preview_rule_until)
                preview_rule_targets = dict(self.preview_rule_targets)
            for context in rule_contexts:
                rule = context["rule"]
                if context["muted"]:
                    continue
                if not context["stop_rule_ids"]:
                    continue
                if self.is_rule_playing(rule["id"]):
                    suppressed_rule_ids.update(context["stop_rule_ids"])
            for preview_rule_id, until_ts in preview_rule_until.items():
                if until_ts > now and self.is_rule_playing(preview_rule_id):
                    suppressed_rule_ids.update(preview_rule_targets.get(preview_rule_id, []))

            with self.lock:
                self.suppressed_rule_ids = set(suppressed_rule_ids)
            if suppressed_rule_ids:
                self.stop_rule_targets(suppressed_rule_ids)
            for context in rule_contexts:
                rule = context["rule"]
                if not rule.get("enabled", True):
                    self.stop_rule_sound(rule.get("id"))
                    continue
                if str(rule["id"]) in preview_rule_until and preview_rule_until[str(rule["id"])] > now:
                    continue
                sound_file = rule.get("sound_file")
                muted = context["muted"]
                gain = max(0.0, min(1.0, float(rule.get("volume_pct") or 100.0) / 100.0))
                stop_rule_ids = context["stop_rule_ids"]
                value = context["value"]
                state = self.rule_state.setdefault(rule["id"], {
                    "last_value": None,
                    "active": False,
                    "last_trigger_ts": 0.0,
                })
                min_value = rule.get("min_value")
                max_value = rule.get("max_value")
                min_delta = float(rule.get("min_delta") or 0.0)
                cooldown = float(rule.get("cooldown_sec") or 10.0)
                in_range = context["in_range"]
                delta = None if state["last_value"] is None else abs(value - state["last_value"])
                changed = state["last_value"] is None or delta >= min_delta
                mode = rule.get("mode", "once")
                is_current = self.is_rule_playing(rule["id"])
                is_chirp = sound_file == "__chirp__"
                try_trigger = in_range and changed and (now - state["last_trigger_ts"] >= cooldown)
                is_suppressed = rule["id"] in suppressed_rule_ids

                if mode == "loop" and not is_chirp:
                    if muted or is_suppressed:
                        if is_current:
                            self.stop_rule_sound(rule["id"])
                        state["active"] = False
                    elif in_range and not is_current and (now - state["last_trigger_ts"] >= cooldown):
                        try:
                            if stop_rule_ids:
                                self.stop_rule_targets(stop_rule_ids)
                            self.play_file(sound_file, loop=True, rule_id=rule["id"], gain=gain)
                            state["last_trigger_ts"] = now
                            state["active"] = True
                        except Exception:
                            pass
                    elif not in_range and is_current:
                        self.stop_rule_sound(rule["id"])
                        state["active"] = False
                    elif not in_range:
                        state["active"] = False
                elif mode == "interval":
                    if muted or is_suppressed:
                        if is_current:
                            self.stop_rule_sound(rule["id"])
                        state["active"] = False
                    elif in_range and not is_current and (now - state["last_trigger_ts"] >= cooldown):
                        try:
                            if stop_rule_ids:
                                self.stop_rule_targets(stop_rule_ids)
                            self.play_file(sound_file, loop=False, rule_id=rule["id"], gain=gain)
                            state["last_trigger_ts"] = now
                            state["active"] = True
                        except Exception:
                            pass
                    elif not in_range:
                        if is_current:
                            self.stop_rule_sound(rule["id"])
                        state["active"] = False
                    elif now - state["last_trigger_ts"] >= cooldown:
                        state["active"] = False
                else:
                    if is_suppressed:
                        if is_current:
                            self.stop_rule_sound(rule["id"])
                        state["active"] = False
                    elif try_trigger and not state["active"]:
                        try:
                            if stop_rule_ids:
                                self.stop_rule_targets(stop_rule_ids)
                            if not muted:
                                self.play_file(sound_file, loop=False, rule_id=rule["id"], gain=gain)
                            state["last_trigger_ts"] = now
                            state["active"] = True
                        except Exception:
                            pass
                    elif not in_range:
                        state["active"] = False
                    elif now - state["last_trigger_ts"] >= cooldown:
                        state["active"] = False
                state["last_value"] = value

sound_engine = SoundEngine()


def compute_forecast_payload():
    latest = get_latest_map()
    fx_config = load_fx_config()
    local_interpretation = compute_local_24h_interpretation()
    pressure = get_metric(latest, "pressure_hpa", "sensor_community_1")
    humidity = get_metric(latest, "rh_pct", "sensor_community_1")
    temperature = get_metric(latest, "temp_c", "sensor_community_1")
    wind = get_metric(latest, "wind_speed_ms", "wind_esp8266")
    rain_rate = get_metric(latest, "rain_rate_mmh", "rain_node_mcu")
    rain_total = get_metric(latest, "rain_mm_total", "rain_node_mcu") or get_metric(latest, "rain_mm", "rain_node_mcu")
    light = get_metric(latest, "light_lux", "light_mcu")
    pm10 = get_metric(latest, "pm10_ugm3", "sensor_community_1")
    pm25 = get_metric(latest, "pm2_5_ugm3", "sensor_community_1")

    pressure_series = get_history("pressure_hpa", "sensor_community_1")
    humidity_series = get_history("rh_pct", "sensor_community_1")
    rain_series = get_history("rain_mm_total", "rain_node_mcu")

    pressure_prev = sample_before(pressure_series, 6 * 3600)
    humidity_prev = sample_before(humidity_series, 6 * 3600)
    rain_prev = sample_before(rain_series, 6 * 3600)

    pressure_trend = None
    humidity_trend = None
    rain_delta = None
    if pressure and pressure_prev:
        pressure_trend = float(pressure["value"]) - float(pressure_prev["value"])
    if humidity and humidity_prev:
        humidity_trend = float(humidity["value"]) - float(humidity_prev["value"])
    if rain_total and rain_prev:
        rain_delta = float(rain_total["value"]) - float(rain_prev["value"])

    label = "Seguimiento local"
    summary = "Datos insuficientes para resumir la tendencia."
    detail = "La estación necesita más histórico para una lectura más clara."
    confidence = "baja"
    icon = "cloud"
    signals = []

    if pressure_trend is not None:
        if pressure_trend <= -3:
            signals.append("presion_bajando")
        elif pressure_trend >= 3:
            signals.append("presion_subiendo")
        else:
            signals.append("presion_estable")
    if humidity_trend is not None:
        if humidity_trend >= 8:
            signals.append("humedad_subiendo")
        elif humidity_trend <= -8:
            signals.append("humedad_bajando")
    if rain_delta is not None and rain_delta > 0.2:
        signals.append("lluvia_reciente")
    if wind and float(wind["value"]) >= 8:
        signals.append("viento_moderado")

    if pressure and humidity:
        confidence = "media"
        if rain_rate and float(rain_rate["value"]) > 0.1:
            label = "Lluvia en curso"
            summary = "Lluvia activa o muy reciente."
            detail = "El pluviómetro ya detecta precipitación. Puede continuar a corto plazo."
            icon = "rain"
        elif pressure_trend is not None and pressure_trend <= -2.5 and float(humidity["value"]) >= 75:
            label = "Inestabilidad probable"
            summary = "Podrían aumentar las nubes y aparecer lluvia débil."
            detail = "La presión cae y la humedad es alta. Eso suele apuntar a tiempo más inestable."
            icon = "storm"
        elif pressure_trend is not None and pressure_trend >= 2.5 and float(humidity["value"]) <= 75:
            label = "Mejora"
            summary = "La tendencia apunta a un tiempo más estable."
            detail = "La presión sube y la humedad no está disparada."
            icon = "sun"
        else:
            label = "Estable"
            summary = "No se esperan cambios bruscos."
            detail = "Las variables se mantienen sin señales fuertes de empeoramiento."
            icon = "cloud"

    pm10_val = float(pm10["value"]) if pm10 else None
    pm25_val = float(pm25["value"]) if pm25 else None
    air_band = "sin_dato"
    air_label = "Sin datos"
    air_color = {"name": "gray", "hex": "#9CA3AF", "rgb": [156, 163, 175]}
    if pm10_val is not None or pm25_val is not None:
        if (pm25_val is not None and pm25_val > 55) or (pm10_val is not None and pm10_val > 100):
            air_band = "muy_mala"
            air_label = "Aire muy cargado"
            air_color = {"name": "red", "hex": "#E74C3C", "rgb": [231, 76, 60]}
        elif (pm25_val is not None and pm25_val > 35) or (pm10_val is not None and pm10_val > 50):
            air_band = "mala"
            air_label = "Aire cargado"
            air_color = {"name": "orange", "hex": "#F39C12", "rgb": [243, 156, 18]}
        elif (pm25_val is not None and pm25_val > 12) or (pm10_val is not None and pm10_val > 20):
            air_band = "regular"
            air_label = "Aire regular"
            air_color = {"name": "yellow", "hex": "#F1C40F", "rgb": [241, 196, 15]}
        else:
            air_band = "buena"
            air_label = "Aire limpio"
            air_color = {"name": "green", "hex": "#2ECC71", "rgb": [46, 204, 113]}

    fx_hint = derive_fx_hint({
        "state": local_interpretation.get("state", {}),
        "forecast": {
            "label": label,
        },
        "air": {
            "band": air_band,
        },
    })
    fx_display = resolve_fx_display(fx_config, fx_hint)

    headline = label
    line1 = summary
    line2 = air_label
    if local_interpretation.get("ok"):
        state = local_interpretation.get("state", {})
        message = local_interpretation.get("message", {})
        display = local_interpretation.get("display", {})
        state_id = state.get("id") or "interpretacion_local"
        label = str(state_id).replace("_", " ").title()
        summary = message.get("phrase") or display.get("line1") or summary
        detail = state.get("detail") or message.get("notes") or detail
        confidence = state.get("confidence") or confidence
        signals = [item.get("condition") for item in local_interpretation.get("ranked_conditions", []) if item.get("condition")]
        headline = summary
        line1 = ""
        line2 = ""
    if len(line1) > 64:
        line1 = line1[:61] + "..."
    if len(line2) > 64:
        line2 = line2[:61] + "..."

    last_ts = max([
        item["ts"] for item in [pressure, humidity, temperature, wind, rain_rate, rain_total, light, pm10, pm25] if item
    ], default=0)

    display_block = {
        "headline": text_for_sign(headline),
        "line1": text_for_sign(line1),
        "line2": text_for_sign(line2),
        "brightness": int(fx_config.get("brightness", 24)),
        "effect": fx_display["effect"],
    }
    web_display_block = {
        "headline": str(headline or ""),
        "line1": str(line1 or ""),
        "line2": str(line2 or ""),
    }

    if fx_display["color_mode"] in FX_COLOR_PRESETS and FX_COLOR_PRESETS[fx_display["color_mode"]]:
        display_block["color"] = FX_COLOR_PRESETS[fx_display["color_mode"]]

    return {
        "ts": time.time(),
        "source_ts": last_ts,
        "forecast": {
            "label": label,
            "summary": summary,
            "detail": detail,
            "confidence": confidence,
            "icon": icon,
            "signals": signals,
        },
        "air": {
            "pm10_ugm3": pm10_val,
            "pm25_ugm3": pm25_val,
            "band": air_band,
            "label": air_label,
            "color": air_color,
        },
        "interpretation": local_interpretation,
        "fx": {
            "config": fx_config,
            "hint": fx_hint,
            "applied": fx_display,
        },
        "display": display_block,
        "web_display": web_display_block,
        "metrics": {
            "temp_c": float(temperature["value"]) if temperature else None,
            "rh_pct": float(humidity["value"]) if humidity else None,
            "pressure_hpa": float(pressure["value"]) if pressure else None,
            "wind_speed_ms": float(wind["value"]) if wind else None,
            "rain_mm_total": float(rain_total["value"]) if rain_total else None,
            "light_lux": float(light["value"]) if light else None,
        },
    }


@app.get("/api/latest")
def api_latest():
    return get_latest_map()


def _run_text_command(command):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip()
    except Exception:
        return None


def get_pi_power_health():
    throttled_raw = _run_text_command(["vcgencmd", "get_throttled"])
    temperature_raw = _run_text_command(["vcgencmd", "measure_temp"])
    core_voltage_raw = _run_text_command(["vcgencmd", "measure_volts", "core"])

    throttled_value = 0
    if throttled_raw and "=" in throttled_raw:
        try:
            throttled_value = int(throttled_raw.split("=", 1)[1], 16)
        except Exception:
            throttled_value = 0

    temperature_c = None
    if temperature_raw and "=" in temperature_raw:
        try:
            temperature_c = float(temperature_raw.split("=", 1)[1].replace("'C", ""))
        except Exception:
            temperature_c = None

    core_voltage_v = None
    if core_voltage_raw and "=" in core_voltage_raw:
        try:
            core_voltage_v = float(core_voltage_raw.split("=", 1)[1].replace("V", ""))
        except Exception:
            core_voltage_v = None

    return {
        "ok": throttled_raw is not None,
        "ts": time.time(),
        "throttled_raw": throttled_raw,
        "current_undervoltage": bool(throttled_value & 0x1),
        "current_freq_capped": bool(throttled_value & 0x2),
        "current_throttled": bool(throttled_value & 0x4),
        "historical_undervoltage": bool(throttled_value & 0x10000),
        "historical_freq_capped": bool(throttled_value & 0x20000),
        "historical_throttled": bool(throttled_value & 0x40000),
        "temperature_c": temperature_c,
        "core_voltage_v": core_voltage_v,
    }


@app.get("/api/pi/health")
def api_pi_health():
    return get_pi_power_health()


@app.get("/api/history")
def api_history(
    metric: str = Query(..., description="metric_id"),
    device: str | None = Query(None, description="device_id"),
    from_ts: str | None = Query(None, alias="from"),
    to_ts: str | None = Query(None, alias="to"),
):
    now = time.time()
    start = parse_timestamp(from_ts, now - 3600)
    end = parse_timestamp(to_ts, now)

    conn = get_conn()
    if device:
        rows = conn.execute(
            "SELECT ts, value, unit, device_id, metric_id FROM readings WHERE metric_id=? AND device_id=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
            (metric, device, start, end),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, value, unit, device_id, metric_id FROM readings WHERE metric_id=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
            (metric, start, end),
        ).fetchall()
    conn.close()

    return [dict(r) for r in rows]


@app.get("/api/rain/window")
def api_rain_window():
    return compute_rain_window_state()


@app.post("/api/rain/window/reset")
def api_rain_window_reset():
    now_ts = time.time()
    save_rain_window_config({
        "anchor_ts": now_ts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    state = compute_rain_window_state(now_ts)
    state["ok"] = True
    return state


@app.get("/api/sign/latest")
def api_sign_latest():
    return compute_forecast_payload()


@app.get("/api/interpretation/local-24h")
def api_interpretation_local_24h():
    return compute_local_24h_interpretation()


@app.get("/api/fx/state")
def api_fx_state():
    config = load_fx_config()
    latest_interp = get_latest_interpretation_history()
    fx_hint = derive_fx_hint({
        "state": {
            "id": latest_interp.get("state_id") if latest_interp else None,
            "confidence": latest_interp.get("confidence") if latest_interp else None,
        },
        "forecast": {
            "label": latest_interp.get("label") if latest_interp else "",
        },
        "air": {},
    })
    return {
        "ok": True,
        "config": config,
        "suggested": {
            "label": latest_interp.get("label") if latest_interp else None,
            "state_id": latest_interp.get("state_id") if latest_interp else None,
            "phrase": latest_interp.get("phrase") if latest_interp else None,
            "hint": fx_hint,
        },
        "presets": [
            {"id": key, "rgb": value}
            for key, value in FX_COLOR_PRESETS.items()
        ],
        "effects": [
            {"id": key, "label": value["label"]}
            for key, value in FX_EFFECT_PRESETS.items()
        ],
    }


@app.get("/api/wind/calibration")
def api_wind_calibration():
    return current_wind_calibration_state()


@app.post("/api/wind/calibration/east")
def api_wind_calibration_east():
    latest = get_latest_map()
    wind_dir = get_metric(latest, "wind_direction_deg", "wind_esp8266")
    raw_deg = metric_float(wind_dir)
    if raw_deg is None:
        raise HTTPException(status_code=400, detail="wind_direction_unavailable")
    calibration = save_wind_calibration({
        "offset_deg": normalize_degrees(90.0 - raw_deg),
        "east_reference_deg": raw_deg,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    corrected_deg = apply_wind_calibration(raw_deg, calibration)
    return {
        "ok": True,
        "calibration": calibration,
        "raw_direction_deg": raw_deg,
        "calibrated_direction_deg": corrected_deg,
        "ts": wind_dir.get("ts") if wind_dir else None,
    }


@app.post("/api/wind/calibration/reset")
def api_wind_calibration_reset():
    calibration = save_wind_calibration({
        "offset_deg": 0.0,
        "east_reference_deg": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "ok": True,
        "calibration": calibration,
        "raw_direction_deg": None,
        "calibrated_direction_deg": None,
        "ts": None,
    }


@app.post("/api/fx/text-color")
def api_fx_text_color(mode: str = Form(...)):
    next_mode = str(mode or "auto").lower().strip()
    if next_mode not in FX_COLOR_PRESETS:
        raise HTTPException(status_code=400, detail="invalid_color_mode")
    config = load_fx_config()
    config["text_color_mode"] = next_mode
    save_fx_config(config)
    return {
        "ok": True,
        "config": config,
        "effects": [
            {"id": key, "label": value["label"]}
            for key, value in FX_EFFECT_PRESETS.items()
        ],
        "presets": [
            {"id": key, "rgb": value}
            for key, value in FX_COLOR_PRESETS.items()
        ],
    }


@app.post("/api/fx/brightness")
def api_fx_brightness(value: int = Form(...)):
    brightness = int(value)
    if brightness < 0 or brightness > 40:
        raise HTTPException(status_code=400, detail="brightness_out_of_range")
    config = load_fx_config()
    config["brightness"] = brightness
    save_fx_config(config)
    return {
        "ok": True,
        "config": config,
        "effects": [
            {"id": key, "label": value["label"]}
            for key, value in FX_EFFECT_PRESETS.items()
        ],
        "presets": [
            {"id": key, "rgb": value}
            for key, value in FX_COLOR_PRESETS.items()
        ],
    }


@app.post("/api/fx/effect")
def api_fx_effect(mode: str = Form(...)):
    next_mode = str(mode or "none").lower().strip()
    if next_mode not in FX_EFFECT_PRESETS:
        raise HTTPException(status_code=400, detail="invalid_effect_mode")
    config = load_fx_config()
    config["effect_mode"] = next_mode
    save_fx_config(config)
    return {
        "ok": True,
        "config": config,
        "effects": [
            {"id": key, "label": value["label"]}
            for key, value in FX_EFFECT_PRESETS.items()
        ],
        "presets": [
            {"id": key, "rgb": value}
            for key, value in FX_COLOR_PRESETS.items()
        ],
    }


@app.get("/api/vapor/state")
def api_vapor_state():
    return vapor_request("GET", "/api/vapor/state")


@app.post("/api/vapor/set")
def api_vapor_set(enabled: bool = Form(...)):
    result = vapor_request("POST", "/api/vapor/set", {
        "enabled": "true" if enabled else "false",
    })
    if result.get("ok") and result.get("online") and result.get("configured"):
        append_vapor_sequence_step("vapor", bool(result.get("relay_on", enabled)))
    return result


@app.get("/api/fan/state")
def api_fan_state():
    return fan_request("GET", "/api/fan/state")


@app.post("/api/fan/set")
def api_fan_set(enabled: bool = Form(...)):
    result = fan_request("POST", "/api/fan/set", {
        "enabled": "true" if enabled else "false",
    })
    if result.get("ok") and result.get("online") and result.get("configured"):
        append_vapor_sequence_step("fan", bool(result.get("relay_on", enabled)))
    return result


@app.get("/api/smoke/state")
def api_smoke_state():
    return smoke_request("GET", "/api/smoke/state")


@app.post("/api/smoke/set")
def api_smoke_set(enabled: bool = Form(...)):
    result = smoke_request("POST", "/api/smoke/set", {
        "enabled": "true" if enabled else "false",
    })
    if result.get("ok") and result.get("online") and result.get("configured"):
        append_vapor_sequence_step("smoke", bool(result.get("relay_on", enabled)))
    return result


@app.get("/api/vapor/sequence")
def api_vapor_sequence(sequence_id: str | None = None):
    return vapor_sequence_status(sequence_id)


@app.post("/api/vapor/sequence/record")
def api_vapor_sequence_record(recording: bool = Form(...), sequence_id: str | None = Form(None)):
    if recording:
        return start_vapor_sequence_recording(sequence_id)
    return stop_vapor_sequence_recording()


@app.post("/api/vapor/sequence/play")
def api_vapor_sequence_play(sequence_id: str | None = Form(None)):
    try:
        return start_vapor_sequence_playback(sequence_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/vapor/sequence/stop")
def api_vapor_sequence_stop():
    stop_vapor_sequence_playback()
    return vapor_sequence_status()


@app.post("/api/vapor/sequence/clear")
def api_vapor_sequence_clear(sequence_id: str | None = Form(None)):
    clear_vapor_sequence(sequence_id)
    return vapor_sequence_status(sequence_id)


@app.post("/api/vapor/sequence/create")
def api_vapor_sequence_create(name: str = Form("Secuencia")):
    return create_vapor_sequence(name)


@app.post("/api/vapor/sequence/select")
def api_vapor_sequence_select(sequence_id: str = Form(...)):
    return select_vapor_sequence(sequence_id)


@app.post("/api/vapor/sequence/rename")
def api_vapor_sequence_rename(sequence_id: str = Form(...), name: str = Form(...)):
    return rename_vapor_sequence(sequence_id, name)


@app.delete("/api/vapor/sequence/{sequence_id}")
def api_vapor_sequence_delete(sequence_id: str):
    return delete_vapor_sequence(sequence_id)


@app.get("/api/vapor/automation")
def api_vapor_automation():
    config = load_vapor_automation_config()
    latest = get_latest_map()
    metrics = []
    for item in sorted(latest.values(), key=lambda row: (row["device_id"], row["metric_id"])):
        metrics.append({
            "device_id": item["device_id"],
            "metric_id": item["metric_id"],
            "label": f"{item['device_id']} / {get_metric_name(item['metric_id'])}",
            "value": item["value"],
            "unit": get_metric_unit(item["metric_id"], item.get("unit")),
        })
    return {
        "ok": True,
        "config": config,
        "metrics": metrics,
        "engine": vapor_automation_engine.status(),
        "sequence": vapor_sequence_status(),
    }


@app.post("/api/vapor/automation/rules")
def api_vapor_automation_rules(payload: dict):
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="rules debe ser una lista")
    normalized = [normalize_vapor_automation_rule(raw) for raw in rules if isinstance(raw, dict)]
    config = load_vapor_automation_config()
    config["rules"] = normalized
    save_vapor_automation_config(config)
    return {"ok": True, "config": config}


@app.delete("/api/vapor/automation/rules/{rule_id}")
def api_vapor_automation_delete_rule(rule_id: str):
    config = load_vapor_automation_config()
    config["rules"] = [rule for rule in config.get("rules", []) if rule.get("id") != rule_id]
    save_vapor_automation_config(config)
    return {"ok": True, "config": config}


@app.post("/api/vapor/automation/rules/{rule_id}/play")
def api_vapor_automation_play_rule(rule_id: str):
    config = load_vapor_automation_config()
    rule = next((item for item in (config.get("rules") or []) if str(item.get("id")) == str(rule_id)), None)
    if not rule:
        raise HTTPException(status_code=404, detail="Regla no encontrada.")
    sequence_id = str(rule.get("sequence_id") or "").strip() or None
    runtime = vapor_sequence_status(sequence_id)
    if runtime.get("recording") or runtime.get("playing"):
        raise HTTPException(status_code=409, detail="Hay una secuencia en curso.")
    vapor_automation_engine.triggering_rule_id = str(rule_id)
    vapor_automation_engine.triggering_sequence_id = sequence_id or runtime.get("sequence_id")
    return start_vapor_sequence_playback(sequence_id)


@app.get("/api/sound/state")
def api_sound_state():
    config = load_sound_config()
    latest = get_latest_map()
    volume = get_sound_volume(config.get("output_device"))
    metrics = []
    for item in sorted(latest.values(), key=lambda row: (row["device_id"], row["metric_id"])):
        metrics.append({
            "device_id": item["device_id"],
            "metric_id": item["metric_id"],
            "label": f"{item['device_id']} / {get_metric_name(item['metric_id'])}",
            "value": item["value"],
            "unit": get_metric_unit(item["metric_id"], item.get("unit")),
        })
    sounds = [{"name": "__chirp__", "size": 0, "label": "Chirp interno"}] + list_sound_files()
    return {
        "config": config,
        "sounds": sounds,
        "engine": sound_engine.status(),
        "rule_runtime": sound_engine.runtime_status(config.get("rules") or []),
        "outputs": {
            "usb": DEFAULT_USB_AUDIO_DEVICE,
            "jack": DEFAULT_JACK_AUDIO_DEVICE,
        },
        "volume": volume,
        "metrics": metrics,
    }


@app.post("/api/sound/global")
def api_sound_global(enabled: bool = Form(...)):
    config = load_sound_config()
    config["enabled"] = bool(enabled)
    save_sound_config(config)
    sound_engine.set_enabled(enabled)
    return {"ok": True, "config": config, "engine": sound_engine.status()}


@app.post("/api/sound/output")
def api_sound_output(device: str = Form(...)):
    device = str(device or "").strip().lower()
    outputs = {
        "usb": DEFAULT_USB_AUDIO_DEVICE,
        "jack": DEFAULT_JACK_AUDIO_DEVICE,
    }
    if device not in outputs:
        raise HTTPException(status_code=400, detail="Salida no soportada.")
    config = load_sound_config()
    config["output_device"] = outputs[device]
    save_sound_config(config)
    sound_engine.stop_sound()
    sound_engine._stop_player_locked()
    return {"ok": True, "config": config, "engine": sound_engine.status(), "volume": get_sound_volume(config.get("output_device"))}


@app.post("/api/sound/volume")
def api_sound_volume(percent: int = Form(...)):
    config = load_sound_config()
    try:
        volume = set_sound_volume(percent, config.get("output_device"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "config": config, "engine": sound_engine.status(), "volume": volume}


@app.post("/api/sound/stop")
def api_sound_stop():
    sound_engine.stop_sound()
    return {"ok": True, "engine": sound_engine.status()}


@app.post("/api/sound/test/chirp")
def api_sound_test_chirp():
    sound_engine.play_chirp()
    return {"ok": True}


@app.post("/api/sound/test/file")
def api_sound_test_file(filename: str = Form(...), loop: bool = Form(False)):
    try:
        sound_engine.play_file(filename, loop=bool(loop), rule_id="manual")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.post("/api/sound/test/rule")
def api_sound_test_rule(rule_id: str = Form(...)):
    config = load_sound_config()
    rule = next((item for item in (config.get("rules") or []) if str(item.get("id")) == str(rule_id)), None)
    if not rule:
        raise HTTPException(status_code=404, detail="Regla no encontrada.")
    filename = str(rule.get("sound_file") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="La regla no tiene sonido asignado.")
    gain = max(0.0, min(1.0, float(rule.get("volume_pct") or 100.0) / 100.0))
    stop_rule_ids = [
        str(value or "").strip()
        for value in (rule.get("stop_rule_ids") or [])
        if str(value or "").strip()
    ]
    if not stop_rule_ids:
        legacy_stop_rule_id = str(rule.get("stop_rule_id") or "").strip() or None
        if legacy_stop_rule_id:
            stop_rule_ids = [legacy_stop_rule_id]
    try:
        sound_engine.start_rule_preview(rule_id, filename, gain=gain, stop_rule_ids=stop_rule_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "rule_id": rule_id}


@app.post("/api/sound/upload")
async def api_sound_upload(file: UploadFile = File(...)):
    os.makedirs(SOUND_DIR, exist_ok=True)
    original = file.filename or "sound.wav"
    ext = os.path.splitext(original)[1].lower()
    if ext not in {".wav", ".mp3", ".ogg"}:
        raise HTTPException(status_code=400, detail="Formato no soportado. Usa wav, mp3 u ogg.")
    safe_name = unique_sound_filename(original)
    path = os.path.join(SOUND_DIR, safe_name)
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"ok": True, "file": {"name": safe_name, "original": original}}


@app.delete("/api/sound/files/{filename}")
def api_sound_delete_file(filename: str):
    if filename == "__chirp__":
        raise HTTPException(status_code=400, detail="El chirp interno no se puede borrar.")
    path = os.path.join(SOUND_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    try:
        os.remove(path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    config = load_sound_config()
    config["rules"] = [rule for rule in config.get("rules", []) if rule.get("sound_file") != filename]
    save_sound_config(config)
    return {"ok": True, "config": config}


@app.post("/api/sound/rules")
def api_sound_rules(payload: dict):
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="rules debe ser una lista")
    previous_config = load_sound_config()
    previous_rules = {
        str(rule.get("id")): normalize_sound_rule(rule)
        for rule in (previous_config.get("rules") or [])
        if isinstance(rule, dict) and rule.get("id")
    }
    normalized = []
    for raw in rules:
        if isinstance(raw, dict):
            normalized.append(normalize_sound_rule(raw))
    config = previous_config
    config["rules"] = normalized
    save_sound_config(config)
    for rule in normalized:
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id:
            continue
        previous = previous_rules.get(rule_id, {})
        muted = bool(rule.get("muted", False))
        enabled = bool(rule.get("enabled", True))
        volume_pct = int(rule.get("volume_pct") or 100)
        gain = max(0.0, min(1.0, float(volume_pct) / 100.0))
        if muted or not enabled:
            sound_engine.stop_rule_sound(rule_id)
        else:
            sound_engine.set_rule_gain(rule_id, gain)
        if (
            muted != bool(previous.get("muted", False))
            or enabled != bool(previous.get("enabled", True))
            or volume_pct != int(previous.get("volume_pct") or 100)
            or str(rule.get("sound_file") or "") != str(previous.get("sound_file") or "")
            or str(rule.get("mode") or "") != str(previous.get("mode") or "")
            or list(rule.get("stop_rule_ids") or []) != list(previous.get("stop_rule_ids") or [])
        ):
            state = sound_engine.rule_state.setdefault(rule_id, {
                "last_value": None,
                "active": False,
                "last_trigger_ts": 0.0,
            })
            state["last_value"] = None
            state["active"] = False
            state["last_trigger_ts"] = 0.0
    return {"ok": True, "config": config}


@app.delete("/api/sound/rules/{rule_id}")
def api_sound_delete_rule(rule_id: str):
    config = load_sound_config()
    config["rules"] = [rule for rule in config.get("rules", []) if rule.get("id") != rule_id]
    save_sound_config(config)
    return {"ok": True, "config": config}


@app.get("/api/stream")
def api_stream(request: Request):
    async def event_stream():
        last_hash = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = json.dumps(list(get_latest_map().values()), separators=(",", ":"))
                h = hash(payload)
                if h != last_hash:
                    last_hash = h
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(SSE_INTERVAL)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/camera")
def api_camera():
    return {
        "host": CAMERA.get("host"),
        "rtsp_url": build_rtsp_url(),
        "rtsp_path": CAMERA.get("rtsp_path"),
        "hls_url": build_hls_url(),
    }


@app.get("/api/camera/status")
def api_camera_status():
    latest = os.path.join(HLS_DIR, "latest.jpg")
    playlist = os.path.join(HLS_DIR, "stream.m3u8")
    now = time.time()
    last = 0.0
    for path in (latest, playlist):
        try:
            m = os.path.getmtime(path)
            if m > last:
                last = m
        except Exception:
            pass
    online = (now - last) <= CAMERA_STALE_SEC if last > 0 else False
    return {"online": online, "last_ts": last}


@app.get("/api/timelapse/status")
def api_timelapse_status():
    return {
        "running": timelapse_running(),
        "interval_sec": timelapse_interval,
    }


@app.post("/api/timelapse/start")
def api_timelapse_start(interval_sec: int = Query(60)):
    start_timelapse(interval_sec)
    return {"ok": True, "running": timelapse_running(), "interval_sec": timelapse_interval}


@app.post("/api/timelapse/stop")
def api_timelapse_stop():
    stop_timelapse()
    return {"ok": True, "running": timelapse_running()}


@app.get("/api/timelapse/list")
def api_timelapse_list(offset: int = 0, limit: int = 40):
    return {"items": list_timelapse(offset, limit)}


@app.post("/api/timelapse/clear")
def api_timelapse_clear():
    clear_timelapse()
    return {"ok": True}


@app.post("/api/timelapse/gif")
def api_timelapse_gif(interval_sec: float = Query(1.0)):
    name = create_timelapse_gif(interval_sec)
    if not name:
        return {"ok": False}
    return {"ok": True, "filename": name}


@app.get("/api/export/status")
def api_export_status():
    return export_status()


@app.post("/api/export/start")
def api_export_start(interval_sec: int = Form(...)):
    return start_export_recording(interval_sec)


@app.get("/api/export/start")
def api_export_start_get(interval_sec: int = Query(5)):
    return start_export_recording(interval_sec)


@app.post("/api/export/stop")
def api_export_stop():
    return stop_export_recording()


@app.get("/api/export/stop")
def api_export_stop_get():
    return stop_export_recording()


@app.get("/api/export/files")
def api_export_files():
    return {"items": list_export_files()}


@app.delete("/api/export/files/{filename}")
def api_export_delete(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(EXPORT_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="export_not_found")
    current_filename = export_runtime.get("current_filename")
    if export_running() and current_filename == safe_name:
        raise HTTPException(status_code=409, detail="export_in_progress")
    os.remove(path)
    return {"ok": True, "filename": safe_name, "items": list_export_files()}


@app.get("/api/export/download/{filename}")
def api_export_download(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(EXPORT_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="export_not_found")
    return FileResponse(path, media_type="text/csv", filename=safe_name)


@app.on_event("startup")
def startup():
    ensure_interpretation_history_table()
    ensure_export_dir()
    sound_engine.start()
    vapor_automation_engine.start()


@app.get("/")
def index():
    rtsp = build_rtsp_url() or ""
    hls = build_hls_url()
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>NUBEMOVIL - ROTOR STUDIO</title>
  <style>
    :root {{
      --sky-1: #eef7ff;
      --sky-2: #d9efff;
      --sky-3: #c7e6ff;
      --sky-4: #b9def9;
      --card-bg: rgba(255, 255, 255, 0.85);
      --card-border: rgba(255, 255, 255, 0.6);
      --ink: #16324a;
      --accent: #3d8ecf;
      --accent-2: #7bb7e6;
    }}
    body {{
      font-family: \"Alegreya Sans\", \"Trebuchet MS\", sans-serif;
      margin: 18px;
      color: var(--ink);
      background:
        linear-gradient(180deg, rgba(238,247,255,0.75), rgba(199,230,255,0.6)),
        url('/static/bg.jpg') center/cover no-repeat fixed;
      min-height: 100vh;
    }}
    h1 {{ letter-spacing: 0.4px; margin-bottom: 10px; }}
    .tabs {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
    .tab {{
      padding: 8px 12px;
      border: 1px solid var(--card-border);
      border-radius: 999px;
      cursor: pointer;
      background: rgba(255,255,255,0.6);
      backdrop-filter: blur(4px);
    }}
    .tab.active {{
      background: rgba(61, 142, 207, 0.2);
      border-color: rgba(61, 142, 207, 0.4);
    }}
    .panel {{ display: none; }}
    .panel.active {{ display: block; }}
    .panel > * {{ margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; align-items: stretch; }}
    .card {{
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px;
      background: var(--card-bg);
      box-shadow: 0 8px 24px rgba(20, 60, 90, 0.08);
    }}
    canvas {{ width: 100%; height: 200px; border: 1px solid rgba(255,255,255,0.6); background: rgba(255,255,255,0.6); border-radius: 10px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, \"Liberation Mono\", monospace; }}
    video {{ width: 100%; max-width: 960px; background: #000; border-radius: 12px; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 720px; background: var(--card-bg); border-radius: 10px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid rgba(22,50,74,0.08); padding: 8px 10px; text-align: left; }}
    th {{ background: rgba(61, 142, 207, 0.12); }}
    .wind-card .row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .compass {{ width: 120px; height: 120px; border: 2px solid rgba(22,50,74,0.6); border-radius: 50%; position: relative; background: rgba(255,255,255,0.6); }}
    .compass .needle {{ position: absolute; left: 50%; top: 10%; width: 2px; height: 45%; background: #c0392b; transform-origin: bottom center; }}
    .compass .center {{ position: absolute; left: 50%; top: 50%; width: 8px; height: 8px; background: #333; border-radius: 50%; transform: translate(-50%, -50%); }}
    .cam-thumb {{ width: 100%; max-width: 240px; border: 1px solid rgba(22,50,74,0.15); border-radius: 10px; margin-top: 6px; }}
    .gps-box {{ display: grid; grid-template-columns: 1fr; gap: 12px; align-items: start; }}
    .gps-map {{ width: 100%; min-height: 260px; border: 1px solid rgba(22,50,74,0.15); border-radius: 10px; background: rgba(255,255,255,0.6); }}
    .gps-actions {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
    .gps-actions button, .gps-actions a {{
      border: 1px solid rgba(61, 142, 207, 0.4);
      background: rgba(255,255,255,0.7);
      color: var(--ink);
      padding: 6px 10px;
      border-radius: 999px;
      text-decoration: none;
      cursor: pointer;
    }}
    .gps-thumb {{ width: 100%; height: 160px; border: 1px solid rgba(22,50,74,0.15); border-radius: 10px; margin-top: 6px; }}
    .timelapse-controls {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .timelapse-controls input {{ width: 80px; padding: 4px 6px; border-radius: 6px; border: 1px solid rgba(22,50,74,0.2); }}
    .timelapse-controls button {{ border: 1px solid rgba(61, 142, 207, 0.4); background: rgba(255,255,255,0.7); color: var(--ink); padding: 6px 10px; border-radius: 999px; cursor: pointer; }}
    .timelapse-gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; max-height: 420px; overflow: auto; padding: 8px; background: rgba(255,255,255,0.6); border-radius: 12px; border: 1px solid rgba(22,50,74,0.15); }}
    .timelapse-gallery img {{ width: 100%; height: auto; border-radius: 8px; border: 1px solid rgba(22,50,74,0.15); }}
    .timelapse-actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .status-line {{ display: flex; align-items: center; gap: 8px; margin: 6px 0 12px; font-size: 0.95em; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; }}
    .dot.online {{ background: #2ecc71; box-shadow: 0 0 6px rgba(46, 204, 113, 0.6); }}
    .dot.offline {{ background: #e74c3c; box-shadow: 0 0 6px rgba(231, 76, 60, 0.4); }}
    .dot.idle {{ background: #f1c40f; box-shadow: 0 0 6px rgba(241, 196, 15, 0.5); }}
    .card-wide {{ grid-column: 1 / -1; }}
    .vapor-panel {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      align-items: stretch;
    }}
    .vapor-pilot {{
      width: 18px;
      height: 18px;
      border-radius: 50%;
      display: inline-block;
      vertical-align: middle;
      margin-right: 8px;
      background: #e74c3c;
      box-shadow: 0 0 10px rgba(231, 76, 60, 0.35);
    }}
    .vapor-pilot.on {{
      background: #2ecc71;
      box-shadow: 0 0 14px rgba(46, 204, 113, 0.5);
    }}
    .vapor-pilot.offline {{
      background: #7f8c8d;
      box-shadow: 0 0 10px rgba(127, 140, 141, 0.35);
    }}
    .vapor-toggle {{
      border: 1px solid rgba(61, 142, 207, 0.45);
      background: linear-gradient(135deg, rgba(61, 142, 207, 0.18), rgba(123, 183, 230, 0.3));
      color: var(--ink);
      font-size: 1.1rem;
      padding: 14px 18px;
      border-radius: 14px;
      cursor: pointer;
      min-width: 180px;
    }}
    .vapor-toggle:disabled {{
      opacity: 0.6;
      cursor: wait;
    }}
    .forecast-card {{
      background:
        radial-gradient(circle at top right, rgba(255, 219, 120, 0.55), transparent 28%),
        linear-gradient(135deg, rgba(37, 113, 186, 0.18), rgba(123, 183, 230, 0.28)),
        rgba(255, 255, 255, 0.92);
      border-color: rgba(61, 142, 207, 0.28);
      min-height: 180px;
    }}
    .forecast-card .forecast-top {{
      display: flex;
      gap: 16px;
      align-items: center;
      margin-bottom: 10px;
      flex-wrap: wrap;
    }}
    .forecast-card .forecast-icon {{
      width: 72px;
      height: 72px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(61, 142, 207, 0.22);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.85);
      flex: 0 0 auto;
    }}
    .forecast-card .forecast-title {{
      font-size: 1.15rem;
      letter-spacing: 0.2px;
    }}
    .forecast-card .forecast-summary {{
      font-size: 1.05rem;
      margin: 6px 0 10px;
    }}
    .forecast-card .forecast-signals {{
      opacity: 0.85;
    }}
    @media (max-width: 720px) {{
      .gps-box {{ grid-template-columns: 1fr; }}
      .forecast-card .forecast-top {{ align-items: flex-start; }}
    }}
  </style>
</head>
<body>
  <h1>NUBEMOVIL - ROTOR STUDIO</h1>
  <div class=\"tabs\">
    <div class=\"tab active\" data-tab=\"dashboard\">Dashboard</div>
    <div class=\"tab\" data-tab=\"wind\">Viento</div>
    <div class=\"tab\" data-tab=\"rain\">Pluviómetro</div>
    <div class=\"tab\" data-tab=\"bme\">Temp/Humedad/Presión/Lux</div>
    <div class=\"tab\" data-tab=\"air\">PM y Aire</div>
    <div class=\"tab\" data-tab=\"gps\">Posición</div>
    <div class=\"tab\" data-tab=\"camera\">Cámara</div>
    <div class=\"tab\" data-tab=\"vapor\">Vapor</div>
    <div class=\"tab\" data-tab=\"fx\">FX</div>
    <div class=\"tab\" data-tab=\"sound\">Sonido</div>
    <div class=\"tab\" data-tab=\"export\">Exportar</div>
    <div class=\"tab\" data-tab=\"forecast\">Interpretación 24h</div>
  </div>

  <div id=\"dashboard\" class=\"panel active\">
    <div id=\"latest\" class=\"grid\">
      <div id=\"dashWind\" class=\"card\"></div>
      <div id=\"dashRain\" class=\"card\"></div>
      <div id=\"dashBme\" class=\"card\"></div>
      <div id=\"dashLight\" class=\"card\"></div>
      <div id=\"dashAir\" class=\"card\"></div>
      <div id=\"dashGps\" class=\"card\"></div>
      <div id=\"dashCam\" class=\"card\"></div>
      <div id=\"dashForecast\" class=\"card forecast-card\"></div>
      <div id=\"dashPower\" class=\"card\"></div>
    </div>
  </div>

  <div id=\"wind\" class=\"panel\">
    <h2>Viento (ESP8266)</h2>
    <div class=\"status-line\">Estado: <span id=\"windStatus\" class=\"dot offline\"></span><span id=\"windStatusText\">Offline</span></div>
    <div class=\"card\">
      <h3>Calibración de dirección</h3>
      <div class=\"timelapse-actions\" style=\"margin-top:8px;\">
        <button onclick=\"calibrateWindEast()\">Calibrar Este</button>
        <button onclick=\"resetWindCalibration()\">Reset</button>
      </div>
      <div id=\"windCalibrationStatus\" style=\"margin-top:10px; opacity:0.85;\">Esperando estado de calibración.</div>
    </div>
    <table>
      <thead><tr><th>Metric</th><th>Value</th><th>Updated</th></tr></thead>
      <tbody id=\"windBody\"></tbody>
    </table>
    <h3>Velocidad del viento (tiempo real)</h3>
    <canvas id=\"windChart\" width=\"700\" height=\"220\"></canvas>
  </div>

  <div id=\"rain\" class=\"panel\">
    <h2>Pluviómetro</h2>
    <div class=\"status-line\">Estado: <span id=\"rainStatus\" class=\"dot offline\"></span><span id=\"rainStatusText\">Offline</span></div>
    <div class=\"card\" style=\"width:240px; max-width:100%;\">
      <div style=\"display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap;\">
        <div style=\"width:100%;\">
          <strong>Lluvia ventana 12h</strong>
          <div id=\"rainWindowAmount\" style=\"margin-top:8px; font-size:1.2rem;\">--</div>
          <div id=\"rainWindowTips\" style=\"margin-top:6px; opacity:0.85;\">Tics: --</div>
          <div id=\"rainWindowRange\" style=\"margin-top:6px; opacity:0.85;\">Ventana: --</div>
          <div id=\"rainWindowNextReset\" style=\"margin-top:6px; opacity:0.85;\">Próximo reinicio: --</div>
          <div id=\"rainWindowMode\" style=\"margin-top:6px; opacity:0.75;\">Modo: --</div>
        </div>
        <div class=\"timelapse-actions\" style=\"margin-top:0;\">
          <button onclick=\"resetRainWindow()\">Reiniciar ventana 12h</button>
        </div>
      </div>
    </div>
    <table>
      <thead><tr><th>Métrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"rainBody\"></tbody>
    </table>
    <h3>Acumulado (24h)</h3>
    <canvas id=\"rainChart\" width=\"700\" height=\"220\"></canvas>
  </div>

  <div id=\"bme\" class=\"panel\">
    <h2>Temperatura / Humedad / Presión / Lux / UV</h2>
    <div class=\"status-line\">Estado: <span id=\"bmeStatus\" class=\"dot offline\"></span><span id=\"bmeStatusText\">Offline</span></div>
    <table>
      <thead><tr><th>Métrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"bmeBody\"></tbody>
    </table>
  </div>

  <div id=\"air\" class=\"panel\">
    <h2>PM10 / PM2.5 y Calidad del Aire</h2>
    <div class=\"status-line\">Estado: <span id=\"airStatus\" class=\"dot offline\"></span><span id=\"airStatusText\">Offline</span></div>
    <table>
      <thead><tr><th>Métrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"airBody\"></tbody>
    </table>
  </div>


  <div id=\"forecast\" class=\"panel\">
    <h2>Interpretación local a 24 horas</h2>
    <div class=\"card\">
      <div id=\"forecastSummaryTitle\"><strong>Calculando...</strong></div>
      <p id=\"forecastSummaryText\">Esperando datos de la estación.</p>
      <p id=\"forecastSummaryDetail\" style=\"opacity:0.85;\"></p>
      <p id=\"forecastConfidence\"></p>
    </div>
    <div class=\"card\">
      <h3>Variables que usamos</h3>
      <div id=\"forecastVariables\"></div>
    </div>
    <div class=\"card\">
      <h3>Cómo se interpreta</h3>
      <div id=\"forecastAlgorithm\"></div>
    </div>
    <div class=\"card\">
      <h3>Señales observadas</h3>
      <div id=\"forecastSignals\"></div>
    </div>
    <div class=\"card\">
      <h3>Histórico reciente</h3>
      <p style=\"opacity:0.8; margin-top:-4px;\">Se guarda una nueva emisión cuando cambia el estado o pasan 3 minutos en estabilidad.</p>
      <div id=\"forecastHistory\"></div>
    </div>
  </div>
  <div id=\"gps\" class=\"panel\">
    <h2>Posición (GPS)</h2>
    <div class=\"status-line\">Estado: <span id=\"gpsStatus\" class=\"dot offline\"></span><span id=\"gpsStatusText\">Offline</span></div>
    <table>
      <thead><tr><th>Métrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"gpsBody\"></tbody>
    </table>
    <div class=\"gps-box\">
      <div id=\"gpsText\">Sin datos</div>
      <div class=\"gps-actions\">
        <button onclick=\"copyGps()\">Copiar coordenadas</button>
        <a id=\"gpsOsmLink\" target=\"_blank\" rel=\"noopener\">Abrir mapa</a>
      </div>
      <iframe id=\"gpsMap\" class=\"gps-map\" loading=\"lazy\"></iframe>
    </div>
  </div>

  <div id=\"vapor\" class=\"panel\">
    <h2>Vaporizadores</h2>
    <div class=\"status-line\">Estado del enlace: <span id=\"vaporLinkStatus\" class=\"dot offline\"></span><span id=\"vaporLinkStatusText\">Offline</span></div>
    <div class=\"vapor-panel\">
      <div class=\"card\">
        <h3>Vapor</h3>
        <p><span id=\"vaporPilot\" class=\"vapor-pilot offline\"></span><strong id=\"vaporStateLabel\">Sin conexión</strong></p>
        <div class=\"timelapse-actions\" style=\"margin-top:12px;\">
          <button id=\"vaporToggleButton\" class=\"vapor-toggle\" onclick=\"toggleVapor()\">Activar vapor</button>
        </div>
        <div id=\"vaporStatusDetail\" style=\"margin-top:12px; opacity:0.85;\">Esperando configuración del relé.</div>
      </div>
      <div class=\"card\">
        <h3>Ventilador</h3>
        <p><span id=\"fanPilot\" class=\"vapor-pilot offline\"></span><strong id=\"fanStateLabel\">Sin conexión</strong></p>
        <div class=\"timelapse-actions\" style=\"margin-top:12px;\">
          <button id=\"fanToggleButton\" class=\"vapor-toggle\" onclick=\"toggleFan()\">Activar ventilador</button>
        </div>
        <div id=\"fanStatusDetail\" style=\"margin-top:12px; opacity:0.85;\">Control independiente del ventilador.</div>
      </div>
      <div class=\"card\">
        <h3>Humo</h3>
        <p><span id=\"smokePilot\" class=\"vapor-pilot offline\"></span><strong id=\"smokeStateLabel\">Sin conexión</strong></p>
        <div class=\"timelapse-actions\" style=\"margin-top:12px;\">
          <button id=\"smokeToggleButton\" class=\"vapor-toggle\" onclick=\"toggleSmoke()\">Activar humo</button>
        </div>
        <div id=\"smokeStatusDetail\" style=\"margin-top:12px; opacity:0.85;\">Control independiente de la máquina de humo.</div>
      </div>
      <div class=\"card\">
        <h3>Secuencia</h3>
        <div id=\"vaporSequenceStatus\" style=\"opacity:0.9;\">Sin secuencia grabada.</div>
        <div style=\"margin-top:12px;\">
          <label>Secuencia activa</label><br>
          <select id=\"vaporSequenceSelect\" style=\"min-width:280px; max-width:100%;\" onchange=\"selectVaporSequence(this.value)\"></select>
        </div>
        <div class=\"timelapse-actions\" style=\"margin-top:10px;\">
          <input id=\"vaporSequenceName\" placeholder=\"Nombre de secuencia\" />
          <button onclick=\"createVaporSequence()\">Nueva</button>
          <button onclick=\"renameVaporSequence()\">Renombrar</button>
          <button onclick=\"deleteVaporSequence()\">Eliminar secuencia</button>
        </div>
        <div id=\"vaporSequenceMeta\" style=\"margin-top:8px; opacity:0.82;\">Pasos: 0 | Duración: 0.0 s</div>
        <div id=\"vaporSequenceInitial\" style=\"margin-top:6px; opacity:0.82;\">Inicio: vapor OFF | ventilador OFF | humo OFF</div>
        <div id=\"vaporSequenceRuntime\" style=\"margin-top:6px; opacity:0.82;\">Grabación: parada | Reproducción: parada</div>
        <div class=\"timelapse-actions\" style=\"margin-top:12px;\">
          <button id=\"vaporSequenceRecordButton\" onclick=\"toggleVaporSequenceRecording()\">Grabar secuencia</button>
          <button id=\"vaporSequencePlayButton\" onclick=\"playVaporSequence()\">Play</button>
          <button id=\"vaporSequenceStopButton\" onclick=\"stopVaporSequencePlayback()\">Stop</button>
          <button id=\"vaporSequenceClearButton\" onclick=\"clearVaporSequence()\">Borrar</button>
        </div>
        <div id=\"vaporSequenceSteps\" class=\"mono\" style=\"margin-top:12px; white-space:pre-wrap; opacity:0.88;\">Sin pasos grabados.</div>
      </div>
      <div class=\"card\">
        <h3>Reglas de disparo</h3>
        <div id=\"vaporAutomationStatus\" style=\"opacity:0.88;\">Sin reglas configuradas.</div>
        <div style=\"margin-top:12px;\">
          <label>Sensor</label><br>
          <select id=\"vaporRuleMetricSelect\" style=\"min-width:320px; max-width:100%;\"></select>
        </div>
        <div style=\"margin-top:10px;\">
          <label>Secuencia</label><br>
          <select id=\"vaporRuleSequenceSelect\" style=\"min-width:320px; max-width:100%;\"></select>
        </div>
        <div style=\"margin-top:10px;\">
          <label>Nombre</label><br>
          <input id=\"vaporRuleName\" placeholder=\"Ej. lluvia activa\" />
        </div>
        <div style=\"display:flex; gap:10px; flex-wrap:wrap; margin-top:10px;\">
          <div>
            <label>Mínimo</label><br>
            <input id=\"vaporRuleMinValue\" type=\"number\" step=\"any\" placeholder=\"sin mínimo\" />
          </div>
          <div>
            <label>Máximo</label><br>
            <input id=\"vaporRuleMaxValue\" type=\"number\" step=\"any\" placeholder=\"sin máximo\" />
          </div>
          <div>
            <label>Cambio mínimo</label><br>
            <input id=\"vaporRuleMinDelta\" type=\"number\" step=\"any\" value=\"0\" />
          </div>
          <div>
            <label>Cooldown s</label><br>
            <input id=\"vaporRuleCooldown\" type=\"number\" step=\"1\" value=\"30\" />
          </div>
        </div>
        <div class=\"timelapse-actions\" style=\"margin-top:12px;\">
          <button onclick=\"addVaporAutomationRule()\">Nueva Regla</button>
        </div>
        <div id=\"vaporRulesList\" style=\"margin-top:12px;\"></div>
      </div>
    </div>
  </div>

  <div id=\"fx\" class=\"panel\">
    <h2>FX</h2>
    <div class=\"card\">
      <h3>Color del texto</h3>
      <p>Por defecto, el cartel usa el color calculado por PM. Desde aquí puedes forzar un color desde la Pi.</p>
      <div class=\"status-line\">Modo actual: <strong id=\"fxColorModeLabel\">auto</strong></div>
      <div class=\"timelapse-actions\" style=\"margin-top:12px; flex-wrap:wrap;\">
        <button onclick=\"setFxTextColor('auto')\">Auto (PM)</button>
        <button onclick=\"setFxTextColor('white')\">Blanco</button>
        <button onclick=\"setFxTextColor('green')\">Verde</button>
        <button onclick=\"setFxTextColor('yellow')\">Amarillo</button>
        <button onclick=\"setFxTextColor('orange')\">Naranja</button>
        <button onclick=\"setFxTextColor('red')\">Rojo</button>
        <button onclick=\"setFxTextColor('blue')\">Azul</button>
        <button onclick=\"setFxTextColor('purple')\">Morado</button>
      </div>
      <div id=\"fxColorPreview\" style=\"margin-top:14px; width:72px; height:18px; border-radius:999px; border:1px solid #999; background:#ddd;\"></div>
      <div style=\"margin-top:12px;\">
        <label for=\"fxBrightnessRange\">Brillo carteles</label>
        <input id=\"fxBrightnessRange\" type=\"range\" min=\"0\" max=\"40\" step=\"1\" value=\"24\" style=\"width:240px; max-width:100%; margin-top:8px; display:block;\" oninput=\"onFxBrightnessPreview(this.value)\" onchange=\"setFxBrightness(this.value)\" />
        <div id=\"fxBrightnessDetail\" style=\"margin-top:6px; opacity:0.85;\">Brillo actual: --</div>
      </div>
      <div id=\"fxStatusDetail\" style=\"margin-top:12px; opacity:0.85;\">Esperando estado.</div>
      <div id=\"fxSuggestedText\" style=\"margin-top:8px; opacity:0.8;\"></div>
    </div>
    <div class=\"card\">
      <h3>Efecto del cartel</h3>
      <p>Selecciona un efecto simple para superponer sobre el texto del cartel.</p>
      <div class=\"status-line\">Efecto actual: <strong id=\"fxEffectModeLabel\">none</strong></div>
      <div class=\"timelapse-actions\" style=\"margin-top:12px; flex-wrap:wrap;\">
        <button onclick=\"setFxEffect('auto')\">Auto (interpretación)</button>
        <button onclick=\"setFxEffect('none')\">Ninguno</button>
        <button onclick=\"setFxEffect('rain')\">Lluvia</button>
        <button onclick=\"setFxEffect('dust')\">Polvo</button>
        <button onclick=\"setFxEffect('lightning')\">Rayo</button>
      </div>
      <div id=\"fxEffectDetail\" style=\"margin-top:12px; opacity:0.85;\">Esperando estado.</div>
    </div>
  </div>

  <div id=\"sound\" class=\"panel\">
    <h2>Sonido</h2>
    <div class=\"card\">
      <div class=\"timelapse-actions\">
        <button onclick=\"toggleSoundGlobal(true)\">Play General</button>
        <button onclick=\"toggleSoundGlobal(false)\">Stop General</button>
        <button onclick=\"stopAllSound()\">Cortar Sonido</button>
        <button onclick=\"playChirpTest()\">Probar Chirp</button>
      </div>
      <div class=\"timelapse-actions\" style=\"margin-top:8px;\">
        <button onclick=\"setSoundOutput('usb')\">Salida USB</button>
        <span id=\"soundOutputUsbStatus\" class=\"dot offline\"></span>
        <button onclick=\"setSoundOutput('jack')\">Salida Jack</button>
        <span id=\"soundOutputJackStatus\" class=\"dot offline\"></span>
      </div>
      <div id=\"soundEngineStatus\" style=\"margin-top:8px; opacity:0.85;\">Estado: --</div>
      <div style=\"margin-top:12px;\">
        <label for=\"soundVolumeRange\">Volumen salida activa</label>
        <input id=\"soundVolumeRange\" type=\"range\" min=\"0\" max=\"100\" step=\"1\" value=\"0\" style=\"width:240px; max-width:100%; margin-top:8px; display:block;\" oninput=\"onSoundVolumePreview(this.value)\" onchange=\"setSoundVolume(this.value)\" />
        <div id=\"soundVolumeStatus\" style=\"margin-top:6px; opacity:0.85;\">Volumen: --</div>
      </div>
    </div>

    <div class=\"card\">
      <h3>Subir sonido</h3>
      <div class=\"timelapse-actions\">
        <input id=\"soundUploadFile\" type=\"file\" accept=\".wav,.mp3,.ogg\" />
        <button onclick=\"uploadSoundFile()\">Subir archivo</button>
      </div>
      <div id=\"soundUploadStatus\" style=\"margin-top:8px; opacity:0.85;\"></div>
    </div>

    <div class=\"card\">
      <h3>Nueva regla</h3>
      <div class=\"timelapse-controls\">
        <label>Nombre: <input id=\"soundRuleName\" type=\"text\" value=\"Regla sonido\" /></label>
        <label>Sensor:
          <select id=\"soundMetricSelect\"></select>
        </label>
        <label>Sonido:
          <select id=\"soundFileSelect\"></select>
        </label>
        <label>Modo:
          <select id=\"soundModeSelect\">
            <option value=\"once\">Una vez</option>
            <option value=\"loop\">Loop</option>
            <option value=\"interval\">Intervalo</option>
          </select>
        </label>
        <label>Min valor: <input id=\"soundMinValue\" type=\"number\" step=\"any\" /></label>
        <label>Max valor: <input id=\"soundMaxValue\" type=\"number\" step=\"any\" /></label>
        <label>Cambio mínimo: <input id=\"soundMinDelta\" type=\"number\" step=\"any\" value=\"1\" /></label>
        <label>Cooldown (s): <input id=\"soundCooldown\" type=\"number\" step=\"1\" value=\"10\" /></label>
        <label>Volumen regla (%): <input id=\"soundRuleVolume\" type=\"number\" min=\"0\" max=\"100\" step=\"1\" value=\"100\" /></label>
        <label>Bloquea regla:
          <select id=\"soundStopRuleSelect\">
            <option value=\"\">Ninguna</option>
          </select>
        </label>
        <label>Bloquea regla 2:
          <select id=\"soundStopRuleSelect2\">
            <option value=\"\">Ninguna</option>
          </select>
        </label>
        <label><input id=\"soundRuleMuted\" type=\"checkbox\" /> Mute</label>
        <button onclick=\"addSoundRule()\">Añadir regla</button>
      </div>
      <div style=\"margin-top:8px; opacity:0.85;\">Una vez: dispara al entrar y con cambio. Loop: suena continuo mientras siga en rango. Intervalo: reproduce una vez cada cooldown mientras siga en rango. Las reglas bloqueadas no volverán a entrar mientras la regla actual siga activa.</div>
    </div>

    <div class=\"card\">
      <h3>Sonidos disponibles</h3>
      <div id=\"soundFilesList\"></div>
    </div>

    <div class=\"card\">
      <h3>Reglas activas</h3>
      <div id=\"soundRulesList\"></div>
    </div>

    <div class=\"card\">
      <h3>API de sonido</h3>
      <div style=\"opacity:0.9;\">
        <p>Esta pestaña usa una API simple para controlar sonidos, subir audios y asociarlos a sensores.</p>
        <p><strong>GET</strong> <span class=\"mono\">/api/sound/state</span>: devuelve reglas, sonidos, motor y métricas disponibles.</p>
        <p><strong>POST</strong> <span class=\"mono\">/api/sound/global</span>: activa o para el motor global de sonido.</p>
        <p><strong>POST</strong> <span class=\"mono\">/api/sound/stop</span>: corta la reproducción actual.</p>
        <p><strong>POST</strong> <span class=\"mono\">/api/sound/test/chirp</span>: lanza el chirp de prueba.</p>
        <p><strong>POST</strong> <span class=\"mono\">/api/sound/test/file</span>: reproduce un archivo subido, en una vez o en loop.</p>
        <p><strong>POST</strong> <span class=\"mono\">/api/sound/upload</span>: sube un <span class=\"mono\">wav</span>, <span class=\"mono\">mp3</span> u <span class=\"mono\">ogg</span>.</p>
        <p><strong>POST</strong> <span class=\"mono\">/api/sound/rules</span>: guarda la lista completa de reglas.</p>
        <p><strong>DELETE</strong> <span class=\"mono\">/api/sound/rules/&lt;id&gt;</span>: elimina una regla.</p>
        <p>Las reglas usan: sensor, sonido, rango mínimo/máximo, cambio mínimo, cooldown y modo <span class=\"mono\">once</span> o <span class=\"mono\">loop</span>.</p>
      </div>
    </div>
  </div>

  <div id=\"export\" class=\"panel\">
    <h2>Exportar datos</h2>
    <div class=\"card\">
      <h3>Grabación de recorrido</h3>
      <div id=\"exportStatus\" style=\"opacity:0.9;\">Estado: parada.</div>
      <div id=\"exportCurrentFile\" style=\"margin-top:8px; opacity:0.85;\">Archivo actual: --</div>
      <div id=\"exportRows\" style=\"margin-top:6px; opacity:0.85;\">Filas: 0</div>
      <div id=\"exportColumns\" style=\"margin-top:6px; opacity:0.85;\">Columnas: --</div>
      <div id=\"exportLastCapture\" style=\"margin-top:6px; opacity:0.85;\">Última captura: --</div>
      <div class=\"timelapse-actions\" style=\"margin-top:12px;\">
        <label>Intervalo (seg): <input id=\"exportInterval\" type=\"number\" min=\"1\" value=\"5\" /></label>
        <button onclick=\"startExportRecording()\">Grabar</button>
        <button onclick=\"stopExportRecording()\">Parar</button>
        <a id=\"exportDownloadCurrent\" class=\"mono\" target=\"_blank\" rel=\"noopener\"></a>
      </div>
      <p style=\"opacity:0.8; margin-top:10px;\">Cada captura guarda todas las métricas actuales y la posición GPS en una fila CSV.</p>
    </div>

    <div class=\"card\">
      <h3>Archivos CSV</h3>
      <div id=\"exportFilesList\">Sin exportaciones todavía.</div>
    </div>
  </div>

  <div id=\"camera\" class=\"panel\">
    <h2>Cámara (Live)</h2>
    <div class=\"status-line\">Estado: <span id=\"cameraStatus\" class=\"dot offline\"></span><span id=\"cameraStatusText\">Offline</span></div>
    <video id=\"video\" controls autoplay muted playsinline></video>
    <img id=\"cameraSnapshot\" alt=\"Vista actual de la camara\" style=\"display:none; width:100%; max-width:960px; background:#000; border-radius:12px; margin-top:8px;\" />
    <p id=\"cameraFallbackText\" style=\"display:none; opacity:0.8;\">El navegador no ha arrancado el HLS; mostrando captura auto-actualizada.</p>
    <p>RTSP URL:</p>
    <div class=\"mono\">{rtsp}</div>
    <p>
      If video does not start, use VLC with the RTSP URL above.
    </p>

    <h3>Timelapse</h3>
    <div class=\"card\">
      <div class=\"timelapse-controls\">
        <label>Intervalo (seg): <input id=\"timelapseInterval\" type=\"number\" min=\"1\" value=\"60\" /></label>
        <button onclick=\"startTimelapse()\">Iniciar</button>
        <button onclick=\"stopTimelapse()\">Parar</button>
        <span id=\"timelapseStatus\">Estado: --</span>
      </div>
    </div>

    <h3>Galería</h3>
    <div id=\"timelapseGallery\" class=\"timelapse-gallery\"></div>

    <h3>Crear GIF</h3>
    <div class=\"card\">
      <div class=\"timelapse-actions\" style=\"margin-top:8px;\">
        <label>Intervalo (seg): <input id=\"gifInterval\" type=\"number\" min=\"1\" value=\"1\" /></label>
        <button onclick=\"createGif()\">Crear GIF</button>
        <a id=\"gifLink\" class=\"mono\" target=\"_blank\" rel=\"noopener\"></a>
      </div>
      <div id=\"gifStatus\" style=\"margin-top:8px; opacity:0.8;\"></div>
      <img id=\"gifPreview\" style=\"width:100%; max-width:520px; margin-top:8px; border-radius:10px; border:1px solid rgba(22,50,74,0.15);\" />
    </div>

    <h3>Gestión de galería</h3>
    <div class=\"card\">
      <div class=\"timelapse-actions\" style=\"margin-top:8px;\">
        <button onclick=\"clearTimelapse()\">Borrar galería</button>
      </div>
    </div>
  </div>

  <script src=\"/static/hls.min.js\"></script>
  <script>
    const METRIC_NAMES = {json.dumps({k: (LABEL_OVERRIDES.get(k) or v.get('name')) for k, v in REGISTRY.get('metrics', {}).items()})};
    const METRIC_UNITS = {json.dumps({k: v.get('unit') for k, v in REGISTRY.get('metrics', {}).items()})};

    const windSeries = [];
    const WIND_MAX_POINTS = 120;

    const WIND_METRIC_PREFIX = 'wind_';
    const ONLINE_MAX_AGE_SEC = 120;
    const STATUS_MAX_AGE_SEC = {{
      wind_esp8266: 120,
      rain_node_mcu: 300,
      bme280_ground: 300,
      sensor_community_1: 600,
      light_mcu: 300,
      gps_usb_1: 180,
    }};

    const DEFAULT_GPS = {{ lat: 43.5550, lon: -5.9240, label: 'Aviles (ejemplo)' }};
    const DASH_CAM_CHECK_MS = 15000;
    let lastCamCheck = 0;
    let lastCamStatus = 'Comprobando...';
    let lastGpsMapSrc = '';
    let lastGpsDashMapSrc = '';
    let lastGpsText = '';
    let lastGpsData = null;
    let latestLoading = false;
    let latestQueued = false;
    let rain24hLoading = false;
    let rainWindowLoading = false;
    let lastRain24hFetch = 0;
    let lastRainWindowFetch = 0;
    const RAIN_HISTORY_REFRESH_MS = 60000;
    const RAIN_WINDOW_REFRESH_MS = 10000;
    let cameraStarted = false;
    let soundUiLoaded = false;
    let timelapseUiLoaded = false;
    let timelapseScrollBound = false;

    function isWindMetric(id) {{
      return id && id.startsWith(WIND_METRIC_PREFIX);
    }}

    function windCardinal(deg) {{
      if (deg === null || deg === undefined || isNaN(deg)) return '--';
      const dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
      const idx = Math.round(((deg % 360) / 22.5)) % 16;
      return dirs[(idx + 16) % 16];
    }}

    function buildWindCard(data) {{
      const entries = Object.values(data).filter(v => isWindMetric(v.metric_id));
      const byId = {{}};
      entries.forEach(v => {{ byId[v.metric_id] = v; }});
      const speed = byId['wind_speed_ms'] ? byId['wind_speed_ms'].value : undefined;
      const dir = byId['wind_direction_deg'] ? byId['wind_direction_deg'].value : undefined;
      const ts = Math.max(byId['wind_speed_ms'] ? byId['wind_speed_ms'].ts : 0, byId['wind_direction_deg'] ? byId['wind_direction_deg'].ts : 0);

      const speedStr = (speed === undefined) ? '--' : Number(speed).toFixed(2);
      const calibratedDeg = applyWindCalibrationDeg(dir);
      const dirStr = (calibratedDeg === null) ? '--' : calibratedDeg.toFixed(0);
      const cardinal = windCardinal(calibratedDeg);
      const unitSpeed = metricUnit('wind_speed_ms', 'm/s');

      const card = document.createElement('div');
      card.className = 'card wind-card';
      const deg = calibratedDeg === null ? 0 : calibratedDeg;
      const tsText = ts ? new Date(ts*1000).toLocaleString() : '';
      card.innerHTML = `
        <strong>${{dotHtml(ts)}} Viento</strong>
        <div>Velocidad: ${{speedStr}} ${{unitSpeed}}</div>
        <div>Dirección: ${{dirStr}}° (${{cardinal}})</div>
        <div class="row">
          <div class="compass">
            <div class="needle" style="transform: translateX(-50%) rotate(${{deg}}deg);"></div>
            <div class="center"></div>
          </div>
          <div><small>${{tsText}}</small></div>
        </div>
      `;
      return card;
    }}

    function setTab(name) {{
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
      document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === name));
      initializeTab(name);
    }}

    document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => setTab(t.dataset.tab)));

    function initializeTab(name) {{
      if (name === 'camera' && !timelapseUiLoaded) {{
        timelapseUiLoaded = true;
        startVideo();
        loadTimelapseStatus();
        setupTimelapseScroll();
        loadMoreTimelapse();
      }}
      if (name === 'sound' && !soundUiLoaded) {{
        soundUiLoaded = true;
        loadSoundState();
      }}
    }}

    function metricLabel(id) {{
      return METRIC_NAMES[id] || id;
    }}

    function isOnlineTs(ts, maxAgeSec = ONLINE_MAX_AGE_SEC) {{
      if (!ts) return false;
      return (Date.now()/1000 - ts) <= maxAgeSec;
    }}

    function dotHtml(ts, maxAgeSec = ONLINE_MAX_AGE_SEC) {{
      return `<span class="dot ${{isOnlineTs(ts, maxAgeSec) ? 'online' : 'offline'}}"></span>`;
    }}

    function metricUnit(id, fallback) {{
      return METRIC_UNITS[id] || fallback || '';
    }}

    function getMetric(data, id) {{
      const matches = Object.values(data).filter(v => v.metric_id === id);
      if (!matches.length) return null;
      matches.sort((a, b) => (b.ts || 0) - (a.ts || 0));
      return matches[0];
    }}

    function getFreshMetric(data, id) {{
      const metric = getMetric(data, id);
      return metric && isOnlineTs(metric.ts) ? metric : null;
    }}

    function max_ts(...items) {{
      return Math.max(...items.map(i => i ? i.ts : 0));
    }}

    function latestTsByDevice(data, deviceId) {{
      const items = Object.values(data).filter(v => v.device_id === deviceId);
      if (!items.length) return 0;
      return Math.max(...items.map(i => i.ts || 0));
    }}

    function setStatus(id, ts, maxAgeSec = ONLINE_MAX_AGE_SEC) {{
      const dot = document.getElementById(id);
      const text = document.getElementById(id + 'Text');
      const online = isOnlineTs(ts, maxAgeSec);
      if (dot) {{
        dot.classList.toggle('online', online);
        dot.classList.toggle('offline', !online);
      }}
      if (text) text.textContent = online ? 'Online' : 'Offline';
    }}

    let vaporState = null;
    let vaporBusy = false;
    let fanState = null;
    let fanBusy = false;
    let smokeState = null;
    let smokeBusy = false;
    let vaporSequenceState = null;
    let fxState = null;
    let exportState = null;
    let windCalibration = {{ calibration: {{ offset_deg: 0.0, east_reference_deg: null, updated_at: null }} }};

    function normalizeWindDeg(deg) {{
      if (deg === null || deg === undefined || Number.isNaN(Number(deg))) return null;
      let value = Number(deg) % 360;
      if (value < 0) value += 360;
      return value;
    }}

    function applyWindCalibrationDeg(deg) {{
      const normalized = normalizeWindDeg(deg);
      if (normalized === null) return null;
      const offset = normalizeWindDeg(windCalibration?.calibration?.offset_deg ?? 0) ?? 0;
      return normalizeWindDeg(normalized + offset);
    }}

    function renderWindCalibrationState(payload) {{
      windCalibration = payload || {{ calibration: {{ offset_deg: 0.0, east_reference_deg: null, updated_at: null }} }};
      const status = document.getElementById('windCalibrationStatus');
      if (!status) return;
      const calibration = windCalibration.calibration || {{}};
      const offset = normalizeWindDeg(calibration.offset_deg ?? 0);
      const eastRef = normalizeWindDeg(calibration.east_reference_deg);
      const corrected = normalizeWindDeg(windCalibration.calibrated_direction_deg);
      const raw = normalizeWindDeg(windCalibration.raw_direction_deg);
      const updatedAt = calibration.updated_at ? new Date(calibration.updated_at).toLocaleString() : 'sin calibrar';
      status.textContent = `Offset: ${{offset === null ? '--' : offset.toFixed(1)}} deg | lectura usada como Este: ${{eastRef === null ? '--' : eastRef.toFixed(1)}} deg | actual corregida: ${{corrected === null ? '--' : corrected.toFixed(1)}} deg | actual raw: ${{raw === null ? '--' : raw.toFixed(1)}} deg | actualizado: ${{updatedAt}}`;
    }}

    async function loadWindCalibration() {{
      try {{
        const res = await fetch('/api/wind/calibration', {{ cache: 'no-store' }});
        const data = await res.json();
        renderWindCalibrationState(data);
      }} catch (_error) {{
        renderWindCalibrationState(null);
      }}
    }}

    async function calibrateWindEast() {{
      const res = await fetch('/api/wind/calibration/east', {{ method: 'POST' }});
      const data = await res.json();
      renderWindCalibrationState(data);
      loadLatest();
    }}

    async function resetWindCalibration() {{
      const res = await fetch('/api/wind/calibration/reset', {{ method: 'POST' }});
      const data = await res.json();
      renderWindCalibrationState(data);
      loadLatest();
    }}

    function renderActuatorControl(prefix, state, busy, labels) {{
      const actuatorState = state || {{}};
      const configured = !!actuatorState.configured;
      const online = !!actuatorState.online;
      const relayOn = !!actuatorState.relay_on;

      const linkDot = document.getElementById(`${{prefix}}LinkStatus`);
      const linkText = document.getElementById(`${{prefix}}LinkStatusText`);
      const pilot = document.getElementById(`${{prefix}}Pilot`);
      const stateLabel = document.getElementById(`${{prefix}}StateLabel`);
      const detail = document.getElementById(`${{prefix}}StatusDetail`);
      const button = document.getElementById(`${{prefix}}ToggleButton`);

      if (linkDot) {{
        linkDot.classList.toggle('online', online);
        linkDot.classList.toggle('offline', !online);
      }}
      if (linkText) {{
        if (!configured) {{
          linkText.textContent = 'No configurado';
        }} else {{
          linkText.textContent = online ? 'Online' : 'Offline';
        }}
      }}

      if (pilot) {{
        pilot.classList.toggle('on', online && relayOn);
        pilot.classList.toggle('offline', !online);
      }}

      if (stateLabel) {{
        if (!configured) {{
          stateLabel.textContent = 'No configurado';
        }} else if (!online) {{
          stateLabel.textContent = 'Sin conexión';
        }} else {{
          stateLabel.textContent = relayOn ? labels.onText : labels.offText;
        }}
      }}

      if (detail) {{
        if (!configured) {{
          detail.textContent = labels.notConfiguredText;
        }} else if (!online) {{
          detail.textContent = `No se pudo contactar con el módulo: ${{actuatorState.reason || 'error desconocido'}}`;
        }} else {{
          detail.textContent = `Módulo operativo en ${{actuatorState.ip || 'IP desconocida'}}. ${{labels.readyText}}`;
        }}
      }}

      if (button) {{
        button.disabled = busy || !configured || !online;
        button.textContent = relayOn ? labels.disableButton : labels.enableButton;
      }}
    }}

    function renderVaporState(data) {{
      vaporState = data || {{}};
      const configured = !!vaporState.configured;
      const online = !!vaporState.online;
      const relayOn = !!vaporState.relay_on;

      const linkDot = document.getElementById('vaporLinkStatus');
      const linkText = document.getElementById('vaporLinkStatusText');
      const pilot = document.getElementById('vaporPilot');
      const stateLabel = document.getElementById('vaporStateLabel');
      const detail = document.getElementById('vaporStatusDetail');
      const button = document.getElementById('vaporToggleButton');

      if (linkDot) {{
        linkDot.classList.toggle('online', online);
        linkDot.classList.toggle('offline', !online);
      }}
      if (linkText) {{
        if (!configured) {{
          linkText.textContent = 'No configurado';
        }} else {{
          linkText.textContent = online ? 'Online' : 'Offline';
        }}
      }}

      if (pilot) {{
        pilot.classList.toggle('on', online && relayOn);
        pilot.classList.toggle('offline', !online);
      }}

      if (stateLabel) {{
        if (!configured) {{
          stateLabel.textContent = 'No configurado';
        }} else if (!online) {{
          stateLabel.textContent = 'Sin conexión';
        }} else {{
          stateLabel.textContent = relayOn ? 'Vapor activado' : 'Vapor apagado';
        }}
      }}

      if (detail) {{
        if (!configured) {{
          detail.textContent = 'Falta configurar la URL o el token del módulo de vapor en la Pi.';
        }} else if (!online) {{
          detail.textContent = `No se pudo contactar con el módulo: ${{vaporState.reason || 'error desconocido'}}`;
        }} else {{
          detail.textContent = `Módulo operativo en ${{vaporState.ip || 'IP desconocida'}}.`;
        }}
      }}

      if (button) {{
        button.disabled = vaporBusy || !configured || !online;
        button.textContent = relayOn ? 'Apagar vapor' : 'Activar vapor';
      }}
      renderVaporSequenceState(vaporSequenceState);
    }}

    async function loadVaporState() {{
      try {{
        const res = await fetch('/api/vapor/state', {{ cache: 'no-store' }});
        const data = await res.json();
        renderVaporState(data);
      }} catch (error) {{
        renderVaporState({{ configured: true, online: false, relay_on: false, reason: 'fetch_failed' }});
      }}
    }}

    async function toggleVapor() {{
      if (!vaporState || !vaporState.configured || !vaporState.online || vaporBusy) {{
        return;
      }}
      vaporBusy = true;
      renderVaporState(vaporState);
      try {{
        const form = new FormData();
        form.append('enabled', vaporState.relay_on ? 'false' : 'true');
        const res = await fetch('/api/vapor/set', {{ method: 'POST', body: form }});
        const data = await res.json();
        renderVaporState(data);
      }} catch (error) {{
        renderVaporState({{ configured: true, online: false, relay_on: false, reason: 'toggle_failed' }});
      }} finally {{
        vaporBusy = false;
        renderVaporState(vaporState);
      }}
    }}

    function renderFxState(data) {{
      fxState = data || {{}};
      const config = fxState.config || {{}};
      const mode = config.text_color_mode || 'auto';
      const effectMode = config.effect_mode || 'none';
      const brightness = Number(config.brightness ?? 24);
      const suggested = fxState.suggested || {{}};
      const hint = suggested.hint || {{}};
      const label = document.getElementById('fxColorModeLabel');
      const detail = document.getElementById('fxStatusDetail');
      const preview = document.getElementById('fxColorPreview');
      const effectLabel = document.getElementById('fxEffectModeLabel');
      const effectDetail = document.getElementById('fxEffectDetail');
      const brightnessRange = document.getElementById('fxBrightnessRange');
      const brightnessDetail = document.getElementById('fxBrightnessDetail');
      if (label) label.textContent = mode;
      if (effectLabel) effectLabel.textContent = effectMode;
      if (brightnessRange) brightnessRange.value = String(brightness);
      if (brightnessDetail) brightnessDetail.textContent = `Brillo actual: ${{brightness}} / 40`;
      if (detail) {{
        detail.textContent = mode === 'auto'
          ? 'El cartel usa el color sugerido por la interpretación local.'
          : `La Pi está forzando el color ${{mode}} en /api/sign/latest.`;
      }}
      if (effectDetail) {{
        if (effectMode === 'auto') {{
          effectDetail.textContent = `La interpretación sugiere ${{hint.effect_mode || 'none'}} porque ${{hint.reason || 'no hay señal dominante'}}.`;
        }} else if (effectMode === 'none') {{
          effectDetail.textContent = 'Sin efecto adicional. Solo texto.';
        }} else if (effectMode === 'rain') {{
          effectDetail.textContent = 'Gotas azules cayendo sobre el cartel.';
        }} else if (effectMode === 'dust') {{
          effectDetail.textContent = 'Polvo naranja repartido por la matriz.';
        }} else {{
          effectDetail.textContent = 'Flash blanco parcial tipo rayo.';
        }}
      }}
      if (suggested && suggested.phrase) {{
        const extra = document.getElementById('fxSuggestedText') || null;
        if (extra) extra.textContent = `Interpretación actual: ${{suggested.state_id || '--'}} · Frase: ${{suggested.phrase}}`;
      }}
      if (preview) {{
        const preset = (fxState.presets || []).find(item => item.id === mode);
        const rgb = preset && preset.rgb ? preset.rgb : null;
        preview.style.background = rgb ? `rgb(${{rgb[0]}}, ${{rgb[1]}}, ${{rgb[2]}})` : 'linear-gradient(90deg, #2ecc71, #f1c40f, #e74c3c)';
      }}
    }}

    async function loadFxState() {{
      try {{
        const res = await fetch('/api/fx/state', {{ cache: 'no-store' }});
        const data = await res.json();
        renderFxState(data);
      }} catch (error) {{
        renderFxState({{ config: {{ text_color_mode: 'auto' }}, presets: [] }});
      }}
    }}

    async function setFxTextColor(mode) {{
      const form = new FormData();
      form.append('mode', mode);
      const res = await fetch('/api/fx/text-color', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderFxState(data);
    }}

    async function setFxEffect(mode) {{
      const form = new FormData();
      form.append('mode', mode);
      const res = await fetch('/api/fx/effect', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderFxState(data);
    }}

    function onFxBrightnessPreview(value) {{
      const detail = document.getElementById('fxBrightnessDetail');
      if (detail) detail.textContent = `Brillo actual: ${{value}} / 40`;
    }}

    async function setFxBrightness(value) {{
      const form = new FormData();
      form.append('value', String(value));
      const res = await fetch('/api/fx/brightness', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderFxState(data);
    }}

    function renderFanState(data) {{
      fanState = data || {{}};
      renderActuatorControl('fan', fanState, fanBusy, {{
        onText: 'Ventilador activado',
        offText: 'Ventilador apagado',
        enableButton: 'Activar ventilador',
        disableButton: 'Apagar ventilador',
        readyText: '',
        notConfiguredText: 'Control del ventilador pendiente de configuración.',
      }});
      renderVaporSequenceState(vaporSequenceState);
    }}

    function renderSmokeState(data) {{
      smokeState = data || {{}};
      renderActuatorControl('smoke', smokeState, smokeBusy, {{
        onText: 'Humo activado',
        offText: 'Humo apagado',
        enableButton: 'Activar humo',
        disableButton: 'Apagar humo',
        readyText: '',
        notConfiguredText: 'Control del humo pendiente de configuración.',
      }});
      renderVaporSequenceState(vaporSequenceState);
    }}

    function formatSequenceSeconds(value) {{
      const numeric = Number(value || 0);
      return numeric.toFixed(1);
    }}

    function renderVaporSequenceState(data) {{
      vaporSequenceState = data || vaporSequenceState || {{}};
      const state = vaporSequenceState || {{}};
      const statusEl = document.getElementById('vaporSequenceStatus');
      const metaEl = document.getElementById('vaporSequenceMeta');
      const initialEl = document.getElementById('vaporSequenceInitial');
      const runtimeEl = document.getElementById('vaporSequenceRuntime');
      const stepsEl = document.getElementById('vaporSequenceSteps');
      const selectEl = document.getElementById('vaporSequenceSelect');
      const nameEl = document.getElementById('vaporSequenceName');
      const recordButton = document.getElementById('vaporSequenceRecordButton');
      const playButton = document.getElementById('vaporSequencePlayButton');
      const stopButton = document.getElementById('vaporSequenceStopButton');
      const clearButton = document.getElementById('vaporSequenceClearButton');
      const stepCount = Number(state.step_count || 0);
      const duration = formatSequenceSeconds(state.duration_sec || 0);
      const recording = !!state.recording;
      const playing = !!state.playing;
      const initial = state.initial || {{}};
      const currentStepIndex = Number.isInteger(state.current_step_index) ? state.current_step_index : null;
      const currentSequenceId = state.sequence_id || state.active_sequence_id || '';
      const sequences = Array.isArray(state.sequences) ? state.sequences : [];

      if (statusEl) {{
        if (recording) statusEl.textContent = 'Grabando secuencia. Usa los botones de vapor, ventilador y humo.';
        else if (playing) statusEl.textContent = 'Reproduciendo secuencia guardada.';
        else if (stepCount > 0) statusEl.textContent = 'Secuencia lista para reproducir.';
        else statusEl.textContent = 'Sin secuencia grabada.';
      }}
      if (selectEl) {{
        const previous = selectEl.value;
        selectEl.innerHTML = sequences.map(item => `<option value="${{item.id}}">${{item.name}} (${{item.step_count}} pasos)</option>`).join('');
        selectEl.value = currentSequenceId || previous;
      }}
      if (nameEl && !nameEl.matches(':focus')) {{
        nameEl.value = state.sequence_name || '';
      }}
      if (metaEl) metaEl.textContent = `Pasos: ${{stepCount}} | Duración: ${{duration}} s`;
      if (initialEl) {{
        initialEl.textContent = `Inicio: vapor ${{initial.vapor ? 'ON' : 'OFF'}} | ventilador ${{initial.fan ? 'ON' : 'OFF'}} | humo ${{initial.smoke ? 'ON' : 'OFF'}}`;
      }}
      if (runtimeEl) {{
        const rec = recording ? `grabando (${{formatSequenceSeconds(state.record_elapsed_sec)}} s)` : 'parada';
        const play = playing ? `reproduciendo (${{formatSequenceSeconds(state.play_elapsed_sec)}} s)` : 'parada';
        runtimeEl.textContent = `Grabación: ${{rec}} | Reproducción: ${{play}}`;
      }}
      if (stepsEl) {{
        const steps = Array.isArray(state.steps) ? state.steps : [];
        if (!steps.length) {{
          stepsEl.textContent = 'Sin pasos grabados.';
        }} else {{
          stepsEl.innerHTML = steps.map((step, index) => {{
            const when = (Number(step.at_ms || 0) / 1000).toFixed(2).padStart(6, ' ');
            const actuator = step.actuator === 'fan' ? 'ventilador' : (step.actuator === 'smoke' ? 'humo' : 'vapor');
            const active = playing && currentStepIndex === index;
            const style = active
              ? 'display:block; padding:3px 6px; margin:2px 0; border-radius:6px; background:rgba(46,204,113,0.22); color:#0b5d1e; font-weight:700;'
              : 'display:block; padding:3px 6px; margin:2px 0; border-radius:6px;';
            return `<span style="${{style}}">${{String(index + 1).padStart(2, '0')}}. +${{when}} s -> ${{actuator}} ${{step.enabled ? 'ON' : 'OFF'}}</span>`;
          }}).join('');
        }}
      }}
      if (recordButton) recordButton.textContent = recording ? 'Parar grabación' : 'Grabar secuencia';
      if (recordButton) recordButton.disabled = playing;
      if (playButton) playButton.disabled = recording || playing || stepCount === 0;
      if (stopButton) stopButton.disabled = !playing;
      if (clearButton) clearButton.disabled = recording || playing || stepCount === 0;

      const disableManual = playing;
      const vaporButton = document.getElementById('vaporToggleButton');
      const fanButton = document.getElementById('fanToggleButton');
      const smokeButton = document.getElementById('smokeToggleButton');
      if (vaporButton && vaporState) {{
        vaporButton.disabled = disableManual || vaporBusy || !vaporState.configured || !vaporState.online;
      }}
      if (fanButton && fanState) {{
        fanButton.disabled = disableManual || fanBusy || !fanState.configured || !fanState.online;
      }}
      if (smokeButton && smokeState) {{
        smokeButton.disabled = disableManual || smokeBusy || !smokeState.configured || !smokeState.online;
      }}
    }}

    async function loadVaporSequenceState(sequenceId = null) {{
      try {{
        const url = sequenceId ? `/api/vapor/sequence?sequence_id=${{encodeURIComponent(sequenceId)}}` : '/api/vapor/sequence';
        const res = await fetch(url, {{ cache: 'no-store' }});
        const data = await res.json();
        renderVaporSequenceState(data);
      }} catch (error) {{
        renderVaporSequenceState({{ recording: false, playing: false, step_count: 0, duration_sec: 0, initial: {{ vapor: false, fan: false, smoke: false }}, steps: [] }});
      }}
    }}

    function currentVaporSequenceId() {{
      return vaporSequenceState?.sequence_id || vaporSequenceState?.active_sequence_id || document.getElementById('vaporSequenceSelect')?.value || '';
    }}

    async function toggleVaporSequenceRecording() {{
      const state = vaporSequenceState || {{}};
      const form = new FormData();
      form.append('recording', state.recording ? 'false' : 'true');
      form.append('sequence_id', currentVaporSequenceId());
      const res = await fetch('/api/vapor/sequence/record', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporState();
      await loadFanState();
      await loadSmokeState();
    }}

    async function playVaporSequence() {{
      const form = new FormData();
      form.append('sequence_id', currentVaporSequenceId());
      const res = await fetch('/api/vapor/sequence/play', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporState();
      await loadFanState();
      await loadSmokeState();
    }}

    async function stopVaporSequencePlayback() {{
      const res = await fetch('/api/vapor/sequence/stop', {{ method: 'POST' }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporState();
      await loadFanState();
      await loadSmokeState();
    }}

    async function clearVaporSequence() {{
      const form = new FormData();
      form.append('sequence_id', currentVaporSequenceId());
      const res = await fetch('/api/vapor/sequence/clear', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderVaporSequenceState(data);
    }}

    async function selectVaporSequence(sequenceId) {{
      const form = new FormData();
      form.append('sequence_id', sequenceId);
      const res = await fetch('/api/vapor/sequence/select', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporAutomationState();
    }}

    async function createVaporSequence() {{
      const form = new FormData();
      form.append('name', document.getElementById('vaporSequenceName')?.value || 'Secuencia');
      const res = await fetch('/api/vapor/sequence/create', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporAutomationState();
    }}

    async function renameVaporSequence() {{
      const form = new FormData();
      form.append('sequence_id', currentVaporSequenceId());
      form.append('name', document.getElementById('vaporSequenceName')?.value || 'Secuencia');
      const res = await fetch('/api/vapor/sequence/rename', {{ method: 'POST', body: form }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporAutomationState();
    }}

    async function deleteVaporSequence() {{
      const sequenceId = currentVaporSequenceId();
      if (!sequenceId) return;
      const res = await fetch(`/api/vapor/sequence/${{encodeURIComponent(sequenceId)}}`, {{ method: 'DELETE' }});
      const data = await res.json();
      renderVaporSequenceState(data);
      await loadVaporAutomationState();
    }}

    function renderVaporAutomationState(payload) {{
      vaporAutomationState = payload || {{}};
      const config = vaporAutomationState.config || {{}};
      const metrics = vaporAutomationState.metrics || [];
      const rules = config.rules || [];
      const engine = vaporAutomationState.engine || {{}};
      const runtimeMap = engine.rule_runtime || {{}};
      const sequence = vaporAutomationState.sequence || {{}};
      const sequences = Array.isArray(sequence.sequences) ? sequence.sequences : [];
      const activeRuleIds = new Set((engine.active_rule_ids || []).map(String));

      const metricSelect = document.getElementById('vaporRuleMetricSelect');
      if (metricSelect) {{
        const current = metricSelect.value;
        metricSelect.innerHTML = metrics.map(item => `<option value="${{metricOptionValue(item)}}">${{item.label}} (${{item.value}} ${{item.unit || ''}})</option>`).join('');
        if (current) metricSelect.value = current;
      }}
      const sequenceSelect = document.getElementById('vaporRuleSequenceSelect');
      if (sequenceSelect) {{
        const current = sequenceSelect.value;
        sequenceSelect.innerHTML = sequences.map(item => `<option value="${{item.id}}">${{item.name}} (${{item.step_count}} pasos)</option>`).join('');
        sequenceSelect.value = current || sequence.sequence_id || sequence.active_sequence_id || '';
      }}

      const status = document.getElementById('vaporAutomationStatus');
      if (status) {{
        if (!sequence.step_count) {{
          status.textContent = 'No hay secuencia grabada. Las reglas no podrán disparar nada hasta guardar una.';
        }} else if (rules.length) {{
          status.textContent = `Reglas: ${{rules.length}} | Motor: ${{engine.running ? 'activo' : 'parado'}} | Secuencia: ${{sequence.step_count}} pasos`;
        }} else {{
          status.textContent = 'Sin reglas configuradas.';
        }}
      }}

      const list = document.getElementById('vaporRulesList');
      if (list) {{
        list.innerHTML = rules.length ? rules.map(rule => `
          ${{(() => {{
            const ruleId = String(rule.id);
            const runtime = runtimeMap[ruleId] || {{}};
            const isActive = activeRuleIds.has(ruleId);
            const isMuted = !!rule.muted;
            const isPlaying = !!runtime.playing;
            const stateText = isPlaying
              ? 'Reproduciendo'
              : (isMuted ? 'Muted' : (isActive ? 'En rango' : (rule.enabled ? 'En espera' : 'Parada')));
            const progressPct = isPlaying
              ? Math.max(0, Math.min(100, Number(runtime.playback_progress_pct || 0)))
              : ((Number(rule.cooldown_sec || 0) > 0 && Number(runtime.cooldown_remaining_sec || 0) > 0)
                  ? Math.max(0, Math.min(100, 100 - ((Number(runtime.cooldown_remaining_sec || 0) / Number(rule.cooldown_sec || 0)) * 100)))
                  : 0);
            const progressColor = isPlaying ? '#22c55e' : (Number(runtime.cooldown_remaining_sec || 0) > 0 ? '#f59e0b' : 'transparent');
            const runtimeText = isPlaying
              ? `Secuencia ${{Number(runtime.play_elapsed_sec || 0).toFixed(1)}} / ${{Number(runtime.play_duration_sec || 0).toFixed(1)}} s`
              : (Number(runtime.cooldown_remaining_sec || 0) > 0 ? `Cooldown ${{Number(runtime.cooldown_remaining_sec || 0).toFixed(1)}} s` : 'Sin actividad reciente');
            const muteStyle = isMuted ? 'background:#ef4444; border-color:#ef4444; color:#fff;' : '';
            const dotStyle = isMuted ? 'background:#ef4444;' : '';
            const muteLabel = isMuted ? 'Unmute' : 'Mute';
            return `
          <div style="margin-bottom:12px; padding-bottom:12px; border-bottom:1px solid rgba(22,50,74,0.08);">
            <strong><span class="dot ${{isActive ? 'online' : 'offline'}}" style="${{dotStyle}}"></span>${{rule.name}}</strong><br>
            Sensor: ${{rule.device_id}} / ${{rule.metric_id}}<br>
            Secuencia: ${{sequences.find(item => item.id === rule.sequence_id)?.name || 'Secuencia activa'}}<br>
            Rango: ${{rule.min_value ?? '--'}} a ${{rule.max_value ?? '--'}} | Cambio mínimo: ${{rule.min_delta}} | Cooldown: ${{rule.cooldown_sec}}s<br>
            Estado: ${{stateText}}<br>
            <div style="margin:6px 0 8px 0; display:inline-block; min-width:220px; max-width:260px;">
              <div style="font-size:12px; opacity:0.86;">${{runtimeText}}</div>
              <div style="margin-top:4px; height:6px; background:rgba(22,50,74,0.12); border-radius:999px; overflow:hidden;">
                <div style="height:100%; width:${{progressPct}}%; background:${{progressColor}};"></div>
              </div>
            </div><br>
            <button onclick="playVaporAutomationRule('${{rule.id}}')">Play</button>
            <button onclick="toggleVaporAutomationRuleMute('${{rule.id}}')" style="${{muteStyle}}">${{muteLabel}}</button>
            <button onclick="removeVaporAutomationRule('${{rule.id}}')">Borrar</button>
          </div>
        `;
          }})()}}
        `).join('') : '<p>Sin reglas configuradas.</p>';
      }}
    }}

    async function loadVaporAutomationState() {{
      const res = await fetch('/api/vapor/automation');
      const data = await res.json();
      renderVaporAutomationState(data);
    }}

    async function addVaporAutomationRule() {{
      const metric = readMetricSelection('vaporRuleMetricSelect');
      if (!metric.device_id || !metric.metric_id) return;
      const nextRules = [ ...(vaporAutomationState?.config?.rules || []) ];
      nextRules.push({{
        id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()),
        name: document.getElementById('vaporRuleName')?.value || `${{metric.device_id}} / ${{metric.metric_id}}`,
        device_id: metric.device_id,
        metric_id: metric.metric_id,
        sequence_id: document.getElementById('vaporRuleSequenceSelect')?.value || currentVaporSequenceId() || null,
        min_value: document.getElementById('vaporRuleMinValue')?.value || null,
        max_value: document.getElementById('vaporRuleMaxValue')?.value || null,
        min_delta: document.getElementById('vaporRuleMinDelta')?.value || 0,
        cooldown_sec: document.getElementById('vaporRuleCooldown')?.value || 30,
        muted: false,
        enabled: true,
      }});
      await fetch('/api/vapor/automation/rules', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ rules: nextRules }}),
      }});
      await loadVaporAutomationState();
    }}

    async function removeVaporAutomationRule(ruleId) {{
      await fetch(`/api/vapor/automation/rules/${{ruleId}}`, {{ method: 'DELETE' }});
      await loadVaporAutomationState();
    }}

    async function toggleVaporAutomationRuleMute(ruleId) {{
      const rules = [ ...(vaporAutomationState?.config?.rules || []) ];
      const nextRules = rules.map(rule => String(rule.id) === String(ruleId)
        ? {{ ...rule, muted: !rule.muted }}
        : rule);
      await fetch('/api/vapor/automation/rules', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ rules: nextRules }}),
      }});
      await loadVaporAutomationState();
    }}

    async function playVaporAutomationRule(ruleId) {{
      const res = await fetch(`/api/vapor/automation/rules/${{ruleId}}/play`, {{ method: 'POST' }});
      if (res.ok) {{
        await loadVaporAutomationState();
        await loadVaporSequenceState();
      }}
    }}

    async function loadFanState() {{
      try {{
        const res = await fetch('/api/fan/state', {{ cache: 'no-store' }});
        const data = await res.json();
        renderFanState(data);
      }} catch (error) {{
        renderFanState({{ configured: true, online: false, relay_on: false, reason: 'fetch_failed' }});
      }}
    }}

    async function toggleFan() {{
      if (!fanState || !fanState.configured || !fanState.online || fanBusy) {{
        return;
      }}
      fanBusy = true;
      renderFanState(fanState);
      try {{
        const form = new FormData();
        form.append('enabled', fanState.relay_on ? 'false' : 'true');
        const res = await fetch('/api/fan/set', {{ method: 'POST', body: form }});
        const data = await res.json();
        renderFanState(data);
      }} catch (error) {{
        renderFanState({{ configured: true, online: false, relay_on: false, reason: 'toggle_failed' }});
      }} finally {{
        fanBusy = false;
        renderFanState(fanState);
      }}
    }}

    async function loadSmokeState() {{
      try {{
        const res = await fetch('/api/smoke/state', {{ cache: 'no-store' }});
        const data = await res.json();
        renderSmokeState(data);
      }} catch (error) {{
        renderSmokeState({{ configured: true, online: false, relay_on: false, reason: 'fetch_failed' }});
      }}
    }}

    async function toggleSmoke() {{
      if (!smokeState || !smokeState.configured || !smokeState.online || smokeBusy) {{
        return;
      }}
      smokeBusy = true;
      renderSmokeState(smokeState);
      try {{
        const form = new FormData();
        form.append('enabled', smokeState.relay_on ? 'false' : 'true');
        const res = await fetch('/api/smoke/set', {{ method: 'POST', body: form }});
        const data = await res.json();
        renderSmokeState(data);
      }} catch (error) {{
        renderSmokeState({{ configured: true, online: false, relay_on: false, reason: 'toggle_failed' }});
      }} finally {{
        smokeBusy = false;
        renderSmokeState(smokeState);
      }}
    }}

    function escapeHtml(value) {{
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function renderExportState(payload) {{
      exportState = payload || {{}};
      const statusEl = document.getElementById('exportStatus');
      const currentFileEl = document.getElementById('exportCurrentFile');
      const rowsEl = document.getElementById('exportRows');
      const columnsEl = document.getElementById('exportColumns');
      const lastCaptureEl = document.getElementById('exportLastCapture');
      const downloadEl = document.getElementById('exportDownloadCurrent');
      const filesEl = document.getElementById('exportFilesList');
      const intervalEl = document.getElementById('exportInterval');

      const running = !!exportState.running;
      const startedAt = exportState.started_at ? new Date(exportState.started_at).toLocaleString() : '--';
      const lastCapture = exportState.last_capture_at ? new Date(exportState.last_capture_at).toLocaleString() : '--';

      if (statusEl) {{
        statusEl.textContent = running
          ? `Estado: grabando cada ${{exportState.interval_sec || 0}} s desde ${{startedAt}}.`
          : 'Estado: parada.';
      }}
      if (currentFileEl) currentFileEl.textContent = `Archivo actual: ${{exportState.current_filename || '--'}}`;
      if (rowsEl) rowsEl.textContent = `Filas: ${{Number(exportState.rows_written || 0)}}`;
      if (columnsEl) columnsEl.textContent = `Columnas: ${{Number(exportState.column_count || 0)}}`;
      if (lastCaptureEl) lastCaptureEl.textContent = `Última captura: ${{lastCapture}}`;
      if (intervalEl && exportState.interval_sec) intervalEl.value = String(exportState.interval_sec);
      if (downloadEl) {{
        if (exportState.current_download_url) {{
          downloadEl.href = exportState.current_download_url;
          downloadEl.textContent = 'Descargar actual';
          downloadEl.style.display = 'inline-block';
        }} else {{
          downloadEl.removeAttribute('href');
          downloadEl.textContent = '';
          downloadEl.style.display = 'none';
        }}
      }}
      if (filesEl) {{
        const files = Array.isArray(exportState.files) ? exportState.files : [];
        if (!files.length) {{
          filesEl.textContent = 'Sin exportaciones todavía.';
        }} else {{
          filesEl.innerHTML = files.map(item => {{
            const ts = item.updated_at ? new Date(item.updated_at).toLocaleString() : '--';
            const kb = (Number(item.size_bytes || 0) / 1024).toFixed(1);
            return `
              <div style="margin-bottom:10px; padding-bottom:10px; border-bottom:1px solid rgba(22,50,74,0.08);">
                <strong>${{escapeHtml(item.filename)}}</strong><br>
                Tamaño: ${{kb}} KB | Actualizado: ${{escapeHtml(ts)}}<br>
                <a class="mono" href="${{item.download_url}}" target="_blank" rel="noopener">Descargar CSV</a>
                <button style="margin-left:10px;" onclick="deleteExportFile('${{encodeURIComponent(item.filename)}}')">Borrar</button>
              </div>
            `;
          }}).join('');
        }}
      }}
    }}

    async function loadExportState() {{
      try {{
        const res = await fetch('/api/export/status', {{ cache: 'no-store' }});
        const data = await res.json();
        renderExportState(data);
      }} catch (_error) {{
        renderExportState({{ running: false, files: [] }});
      }}
    }}

    async function startExportRecording() {{
      const statusEl = document.getElementById('exportStatus');
      const intervalInput = document.getElementById('exportInterval');
      const intervalSec = (intervalInput && intervalInput.value) ? intervalInput.value : '5';
      if (statusEl) statusEl.textContent = `Estado: iniciando grabación cada ${{intervalSec}} s...`;
      try {{
        const params = new URLSearchParams();
        params.set('interval_sec', intervalSec);
        let res = await fetch('/api/export/start', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
          body: params.toString(),
        }});
        if (!res.ok) {{
          res = await fetch(`/api/export/start?interval_sec=${{encodeURIComponent(intervalSec)}}`, {{ cache: 'no-store' }});
        }}
        const data = await res.json();
        renderExportState(data);
      }} catch (error) {{
        if (statusEl) statusEl.textContent = `Estado: error al iniciar (${{error}}).`;
      }}
    }}

    async function stopExportRecording() {{
      const statusEl = document.getElementById('exportStatus');
      if (statusEl) statusEl.textContent = 'Estado: deteniendo grabación...';
      try {{
        let res = await fetch('/api/export/stop', {{ method: 'POST' }});
        if (!res.ok) {{
          res = await fetch('/api/export/stop', {{ cache: 'no-store' }});
        }}
        const data = await res.json();
        renderExportState(data);
      }} catch (error) {{
        if (statusEl) statusEl.textContent = `Estado: error al parar (${{error}}).`;
      }}
    }}

    async function deleteExportFile(encodedFilename) {{
      const statusEl = document.getElementById('exportStatus');
      if (!encodedFilename) return;
      try {{
        const res = await fetch(`/api/export/files/${{encodedFilename}}`, {{ method: 'DELETE' }});
        const data = await res.json();
        if (!res.ok) {{
          throw new Error(data?.detail || 'delete_failed');
        }}
        if (statusEl) statusEl.textContent = 'Estado: archivo borrado.';
        renderExportState({{ ...(exportState || {{}}), files: Array.isArray(data.items) ? data.items : [] }});
        await loadExportState();
      }} catch (error) {{
        if (statusEl) statusEl.textContent = `Estado: error al borrar (${{error}}).`;
      }}
    }}

    function buildSummaryCard(title, rows, ts) {{
      const card = document.createElement('div');
      card.className = 'card';
      const timeText = ts ? new Date(ts*1000).toLocaleString() : '';
      const rowsHtml = rows.map(r => `<div>${{r[0]}}: ${{r[1]}}</div>`).join('');
      card.innerHTML = `<strong>${{title}}</strong>${{rowsHtml}}<div><small>${{timeText}}</small></div>`;
      return card;
    }}

    function setCard(el, title, rows, ts, maxAgeSec = ONLINE_MAX_AGE_SEC) {{
      if (!el) return;
      const timeText = ts ? new Date(ts*1000).toLocaleString() : '';
      const rowsHtml = rows.map(r => `<div>${{r[0]}}: ${{r[1]}}</div>`).join('');
      el.innerHTML = `<strong>${{dotHtml(ts, maxAgeSec)}} ${{title}}</strong>${{rowsHtml}}<div><small>${{timeText}}</small></div>`;
    }}

    function updateCameraThumb() {{
      const img = document.getElementById(\"camThumb\");
      if (!img) return;
      img.src = `/hls/latest.jpg?ts=${{Date.now()}}`;
    }}

    function updateCameraStatus() {{
      const now = Date.now();
      if (now - lastCamCheck < DASH_CAM_CHECK_MS) return;
      lastCamCheck = now;
      fetch('/api/camera/status', {{ method: 'GET', cache: 'no-store' }})
        .then(r => r.json())
        .then(data => {{
          const online = !!data.online;
          lastCamStatus = online ? 'Online' : 'Offline';
          const el = document.getElementById('camStatus');
          if (el) el.textContent = lastCamStatus;
          const dot = document.getElementById('cameraStatus');
          const text = document.getElementById('cameraStatusText');
          const dashDot = document.getElementById('dashCamDot');
          if (dot) {{
            dot.classList.toggle('online', online);
            dot.classList.toggle('offline', !online);
          }}
          if (dashDot) {{
            dashDot.classList.toggle('online', online);
            dashDot.classList.toggle('offline', !online);
          }}
          if (text) text.textContent = online ? 'Online' : 'Offline';
        }})
        .catch(() => {{
          lastCamStatus = 'Offline';
          const el = document.getElementById('camStatus');
          if (el) el.textContent = lastCamStatus;
          const dot = document.getElementById('cameraStatus');
          const text = document.getElementById('cameraStatusText');
          const dashDot = document.getElementById('dashCamDot');
          if (dot) {{
            dot.classList.toggle('online', false);
            dot.classList.toggle('offline', true);
          }}
          if (dashDot) {{
            dashDot.classList.toggle('online', false);
            dashDot.classList.toggle('offline', true);
          }}
          if (text) text.textContent = 'Offline';
        }});
    }}

    function renderDashWind(data) {{
      const entries = Object.values(data).filter(v => isWindMetric(v.metric_id));
      const byId = {{}};
      entries.forEach(v => {{ byId[v.metric_id] = v; }});
      const speed = byId['wind_speed_ms'] ? byId['wind_speed_ms'].value : undefined;
      const dir = byId['wind_direction_deg'] ? byId['wind_direction_deg'].value : undefined;
      const ts = Math.max(byId['wind_speed_ms'] ? byId['wind_speed_ms'].ts : 0, byId['wind_direction_deg'] ? byId['wind_direction_deg'].ts : 0);

      const speedStr = (speed === undefined) ? '--' : Number(speed).toFixed(2);
      const calibratedDeg = applyWindCalibrationDeg(dir);
      const dirStr = (calibratedDeg === null) ? '--' : calibratedDeg.toFixed(0);
      const cardinal = windCardinal(calibratedDeg);
      const unitSpeed = metricUnit('wind_speed_ms', 'm/s');
      const deg = calibratedDeg === null ? 0 : calibratedDeg;
      const tsText = ts ? new Date(ts*1000).toLocaleString() : '';

      const el = document.getElementById('dashWind');
      if (!el) return;
      el.innerHTML = `
        <strong>${{dotHtml(ts)}} Viento</strong>
        <div>Velocidad: ${{speedStr}} ${{unitSpeed}}</div>
        <div>Dirección: ${{dirStr}}° (${{cardinal}})</div>
        <div class="row">
          <div class="compass">
            <div class="needle" style="transform: translateX(-50%) rotate(${{deg}}deg);"></div>
            <div class="center"></div>
          </div>
          <div><small>${{tsText}}</small></div>
        </div>
      `;
    }}

    function buildGpsCard(data) {{
      const lat = getMetric(data, 'gps_lat');
      const lon = getMetric(data, 'gps_lon');
      const alt = getMetric(data, 'gps_alt');

      const latv = lat ? Number(lat.value) : DEFAULT_GPS.lat;
      const lonv = lon ? Number(lon.value) : DEFAULT_GPS.lon;
      const altv = alt ? Number(alt.value) : null;
      const label = lat && lon ? 'Posición (GPS)' : DEFAULT_GPS.label;

      const card = document.createElement('div');
      card.className = 'card';
      const delta = 0.01;
      const bbox = `${{lonv-delta}},${{latv-delta}},${{lonv+delta}},${{latv+delta}}`;
      const mapSrc = `https://www.openstreetmap.org/export/embed.html?bbox=${{bbox}}&layer=mapnik&marker=${{latv}},${{lonv}}`;
      const line1 = `Lat: ${{latv.toFixed(6)}}  Lon: ${{lonv.toFixed(6)}}`;
      const line2 = altv !== null ? `Alt: ${{altv.toFixed(1)}} m` : '';
      card.innerHTML = `<strong>${{label}}</strong><div>${{line1}}</div><div>${{line2}}</div><iframe class="gps-thumb" src="${{mapSrc}}" loading="lazy"></iframe>`;
      return card;
    }}

    function buildGpsMapSrc(latv, lonv) {{
      const delta = 0.01;
      const bbox = `${{lonv-delta}},${{latv-delta}},${{lonv+delta}},${{latv+delta}}`;
      return `https://www.openstreetmap.org/export/embed.html?bbox=${{bbox}}&layer=mapnik&marker=${{latv}},${{lonv}}`;
    }}

    function renderGPS(data) {{
      const lat = getMetric(data, 'gps_lat');
      const lon = getMetric(data, 'gps_lon');
      const alt = getMetric(data, 'gps_alt');
      const latv = lat ? Number(lat.value) : DEFAULT_GPS.lat;
      const lonv = lon ? Number(lon.value) : DEFAULT_GPS.lon;
      const altv = alt ? Number(alt.value) : null;
      const label = lat && lon ? 'Posición GPS' : DEFAULT_GPS.label;
      const mapSrc = buildGpsMapSrc(latv, lonv);
      const text = altv !== null
        ? `${{label}} - Lat ${{latv.toFixed(6)}}, Lon ${{lonv.toFixed(6)}}, Alt ${{altv.toFixed(1)}} m`
        : `${{label}} - Lat ${{latv.toFixed(6)}}, Lon ${{lonv.toFixed(6)}}`;
      lastGpsData = {{ lat: latv, lon: lonv, alt: altv, label }};
      const textEl = document.getElementById('gpsText');
      if (textEl && text !== lastGpsText) {{
        textEl.textContent = text;
        lastGpsText = text;
      }}
      const mapEl = document.getElementById('gpsMap');
      if (mapEl && mapSrc !== lastGpsMapSrc) {{
        mapEl.src = mapSrc;
        lastGpsMapSrc = mapSrc;
      }}
      const linkEl = document.getElementById('gpsOsmLink');
      if (linkEl) linkEl.href = `https://www.openstreetmap.org/?mlat=${{latv}}&mlon=${{lonv}}#map=16/${{latv}}/${{lonv}}`;
    }}

    function copyGps() {{
      if (!lastGpsData) return;
      const text = `${{lastGpsData.lat.toFixed(6)}}, ${{lastGpsData.lon.toFixed(6)}}`;
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).catch(() => {{}});
        return;
      }}
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {{ document.execCommand('copy'); }} catch (e) {{}}
      document.body.removeChild(ta);
    }}

    function renderDashGps(data) {{
      const lat = getMetric(data, 'gps_lat');
      const lon = getMetric(data, 'gps_lon');
      const alt = getMetric(data, 'gps_alt');
      const latv = lat ? Number(lat.value) : DEFAULT_GPS.lat;
      const lonv = lon ? Number(lon.value) : DEFAULT_GPS.lon;
      const altv = alt ? Number(alt.value) : null;
      const label = 'Localización';
      const mapSrc = buildGpsMapSrc(latv, lonv);
      const line1 = `Lat: ${{latv.toFixed(6)}}  Lon: ${{lonv.toFixed(6)}}`;
      const line2 = altv !== null ? `Alt: ${{altv.toFixed(1)}} m` : '';
      const ts = max_ts(lat, lon, alt);

      const el = document.getElementById('dashGps');
      if (!el) return;
      if (!el.dataset.ready) {{
        el.innerHTML = `<strong>${{dotHtml(ts)}} ${{label}}</strong><div id="dashGpsLine1"></div><div id="dashGpsLine2"></div><iframe id="gpsDashMap" class="gps-thumb" loading="lazy"></iframe>`;
        el.dataset.ready = '1';
      }}
      const l1 = document.getElementById('dashGpsLine1');
      const l2 = document.getElementById('dashGpsLine2');
      if (l1) l1.textContent = line1;
      if (l2) l2.textContent = line2;
      const mapEl = document.getElementById('gpsDashMap');
      if (mapEl && mapSrc !== lastGpsDashMapSrc) {{
        mapEl.src = mapSrc;
        lastGpsDashMapSrc = mapSrc;
      }}
      const titleEl = el.querySelector('strong');
      if (titleEl) titleEl.innerHTML = `${{dotHtml(ts)}} ${{label}}`;
    }}

    function renderDashCam() {{
      const el = document.getElementById('dashCam');
      if (!el) return;
      if (!el.dataset.ready) {{
        el.innerHTML = `<strong><span id="dashCamDot" class="dot offline"></span> Cámara</strong><div>Estado: <span id="camStatus">${{lastCamStatus}}</span></div><img id="camThumb" class="cam-thumb" src="/hls/latest.jpg" /><div><small>Ver pestaña Cámara</small></div>`;
        el.dataset.ready = '1';
      }}
      updateCameraStatus();
    }}

    function renderSimpleTable(bodyId, metricIds, data) {{
      const body = document.getElementById(bodyId);
      if (!body) return;
      body.innerHTML = '';
      let rows = 0;
      metricIds.forEach(id => {{
        const m = getMetric(data, id);
        if (!m) return;
        const label = metricLabel(id);
        const unit = metricUnit(id, m.unit);
        let value = m.value;
        if (id === 'wind_direction_cardinal') {{
          const deg = getMetric(data, 'wind_direction_deg');
          value = windCardinal(deg ? Number(deg.value) : NaN);
        }}
        if (m.device_id === 'sensor_community_1' && typeof value === 'number') {{
          value = value.toFixed(2);
        }}
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${{dotHtml(m.ts)}} ${{label}}</td><td>${{value}} ${{unit}}</td><td>${{new Date(m.ts*1000).toLocaleString()}}</td>`;
        body.appendChild(tr);
        rows += 1;
      }});
      if (rows === 0) {{
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="3" style="opacity:0.6;">Sin datos</td>`;
        body.appendChild(tr);
      }}
    }}

    function buildCameraCard() {{
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `<strong>Cámara</strong><div>Estado: <span id="camStatus">Comprobando...</span></div><img id="camThumb" class="cam-thumb" src="/hls/latest.jpg" /><div><small>Ver pestaña Cámara</small></div>`;
      updateCameraStatus();
      return card;
    }}


let lastForecastFetch = 0;
let lastForecastData = null;
const FORECAST_REFRESH_MS = 5000;

function latestMetricById(data, metricId, preferredDevice) {{
  const items = Object.values(data).filter(v => v.metric_id === metricId);
  if (!items.length) return null;
  if (preferredDevice) {{
    const preferred = items.find(v => v.device_id === preferredDevice);
    if (preferred) return preferred;
  }}
  items.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return items[0] || null;
}}

function sampleBefore(points, secondsAgo) {{
  if (!points || !points.length) return null;
  const cutoff = Date.now()/1000 - secondsAgo;
  let candidate = null;
  for (const item of points) {{
    if (item.ts <= cutoff) candidate = item;
    else break;
  }}
  return candidate || points[0];
}}

function formatMaybe(value, digits = 1) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'sin dato';
  return Number(value).toFixed(digits);
}}

function forecastIcon(label) {{
  if (label === 'Lluvia en curso') {{
    return `<svg viewBox="0 0 72 72" width="52" height="52" aria-hidden="true">
      <circle cx="24" cy="27" r="12" fill="#f6c453"></circle>
      <g fill="#6ba7d6">
        <circle cx="36" cy="35" r="12"></circle>
        <circle cx="47" cy="35" r="10"></circle>
        <circle cx="27" cy="39" r="9"></circle>
        <rect x="24" y="35" width="30" height="12" rx="6"></rect>
      </g>
      <g stroke="#2b6cb0" stroke-width="4" stroke-linecap="round">
        <line x1="28" y1="52" x2="24" y2="60"></line>
        <line x1="40" y1="52" x2="36" y2="60"></line>
        <line x1="52" y1="52" x2="48" y2="60"></line>
      </g>
    </svg>`;
  }}
  if (label === 'Inestabilidad probable') {{
    return `<svg viewBox="0 0 72 72" width="52" height="52" aria-hidden="true">
      <circle cx="23" cy="24" r="11" fill="#ffd45f"></circle>
      <g fill="#7aa9d6">
        <circle cx="36" cy="35" r="12"></circle>
        <circle cx="48" cy="35" r="10"></circle>
        <circle cx="28" cy="39" r="9"></circle>
        <rect x="24" y="35" width="32" height="12" rx="6"></rect>
      </g>
      <path d="M37 47 L31 60 L39 60 L34 69 L48 52 L40 52 L45 47 Z" fill="#f4b942"></path>
    </svg>`;
  }}
  if (label === 'Mejora') {{
    return `<svg viewBox="0 0 72 72" width="52" height="52" aria-hidden="true">
      <circle cx="36" cy="36" r="14" fill="#ffd45f"></circle>
      <g stroke="#f4b942" stroke-width="4" stroke-linecap="round">
        <line x1="36" y1="10" x2="36" y2="20"></line>
        <line x1="36" y1="52" x2="36" y2="62"></line>
        <line x1="10" y1="36" x2="20" y2="36"></line>
        <line x1="52" y1="36" x2="62" y2="36"></line>
        <line x1="18" y1="18" x2="24" y2="24"></line>
        <line x1="48" y1="48" x2="54" y2="54"></line>
        <line x1="18" y1="54" x2="24" y2="48"></line>
        <line x1="48" y1="24" x2="54" y2="18"></line>
      </g>
    </svg>`;
  }}
  return `<svg viewBox="0 0 72 72" width="52" height="52" aria-hidden="true">
    <circle cx="25" cy="26" r="12" fill="#ffd45f"></circle>
    <g fill="#89b7de">
      <circle cx="37" cy="37" r="12"></circle>
      <circle cx="48" cy="37" r="10"></circle>
      <circle cx="28" cy="41" r="9"></circle>
      <rect x="25" y="37" width="31" height="12" rx="6"></rect>
    </g>
  </svg>`;
}}

async function fetchHistoryMetric(metric, device) {{
  let url = `/api/history?metric=${{encodeURIComponent(metric)}}`;
  if (device) url += `&device=${{encodeURIComponent(device)}}`;
  const res = await fetch(url);
  return await res.json();
}}

async function buildForecast(data) {{
  try {{
    const res = await fetch('/api/interpretation/local-24h');
    const payload = await res.json();
    if (res.ok && payload && payload.ok) {{
      const state = payload.state || {{}};
      const message = payload.message || {{}};
      const metrics = payload.metrics || {{}};
      const ranked = payload.ranked_conditions || [];
      const label = state.id ? state.id.replace(/_/g, ' ') : 'Interpretación local';
      return {{
        label,
        summary: message.phrase || state.summary || 'Sin mensaje disponible.',
        detail: state.detail || message.notes || '',
        confidence: state.confidence || 'baja',
        history: Array.isArray(payload.history) ? payload.history : [],
        signals: ranked.slice(0, 5).map(item => `${{item.condition}} (${{item.score}}): ${{(item.evidence || []).join(' / ')}}`),
        variables: [
          {{ name: 'Estado lógico elegido', value: state.id || 'sin dato', description: state.summary || '' }},
          {{ name: 'Frase emitible', value: message.phrase || 'sin dato', description: `${{message.category || ''}} ${{message.type || ''}}`.trim() }},
          {{ name: 'Presión / tendencia', value: metrics.pressure_hpa != null ? `${{formatMaybe(metrics.pressure_hpa, 1)}} hPa / ${{formatMaybe(metrics.pressure_trend_6h, 1)}} hPa en 6h` : 'sin dato', description: 'Ayuda a valorar estabilidad o cambio de tiempo.' }},
          {{ name: 'Humedad / tendencia', value: metrics.rh_pct != null ? `${{formatMaybe(metrics.rh_pct, 1)}} % / ${{formatMaybe(metrics.humidity_trend_6h, 1)}} puntos en 6h` : 'sin dato', description: 'Define ambiente seco, cargado, niebla probable o continuidad de lluvia.' }},
          {{ name: 'Lluvia', value: metrics.rain_rate_mmh != null ? `${{formatMaybe(metrics.rain_rate_mmh, 2)}} mm/h; delta 6h ${{formatMaybe(metrics.rain_delta_6h, 2)}} mm` : 'sin dato', description: 'Señal directa para lluvia activa o reciente.' }},
          {{ name: 'Viento', value: metrics.wind_speed_ms != null ? `${{formatMaybe(metrics.wind_speed_ms, 1)}} m/s` : 'sin dato', description: 'Refuerza estados de cambio, incomodidad o lluvia con viento.' }},
          {{ name: 'Luz / UV', value: [metrics.light_lux != null ? `${{formatMaybe(metrics.light_lux, 0)}} lux` : null, metrics.uv_raw != null ? `UV raw ${{formatMaybe(metrics.uv_raw, 0)}}` : null, metrics.uv_voltage_v != null ? `${{formatMaybe(metrics.uv_voltage_v, 2)}} V` : null].filter(Boolean).join(', ') || 'sin dato', description: 'Luminosidad calibrada: tapado ~1 lux, interior ~168 lux, móvil cerca ~1014 lux. El UV queda en observación porque raw/voltaje apenas varían.' }},
          {{ name: 'Aire', value: [metrics.pm10_ugm3 != null ? `PM10 ${{formatMaybe(metrics.pm10_ugm3, 1)}}` : null, metrics.pm2_5_ugm3 != null ? `PM2.5 ${{formatMaybe(metrics.pm2_5_ugm3, 1)}}` : null].filter(Boolean).join(', ') || 'sin dato', description: 'Contexto ambiental para aire limpio, regular o cargado.' }},
        ],
        algorithm: [
          'El backend convierte sensores en estados lógicos puntuados.',
          'Cada estado acumula evidencias: lluvia, humedad, presión, viento, luz, UV, partículas y suelo.',
          'El estado con más puntuación busca una frase del CSV con la misma condicion_logica.',
          'Si no hay frase exacta, usa mensajes generales para no dejar los carteles vacíos.',
        ],
      }};
    }}
  }} catch (_err) {{}}

  const pressure = latestMetricById(data, 'pressure_hpa', 'sensor_community_1');
  const humidity = latestMetricById(data, 'rh_pct', 'sensor_community_1');
  const temperature = latestMetricById(data, 'temp_c', 'sensor_community_1');
  const wind = latestMetricById(data, 'wind_speed_ms', 'wind_esp8266');
  const rainRate = latestMetricById(data, 'rain_rate_mmh', 'rain_node_mcu');
  const rainTotal = latestMetricById(data, 'rain_mm_total', 'rain_node_mcu');
  const light = latestMetricById(data, 'light_lux', 'light_mcu');
  const pm10 = latestMetricById(data, 'pm10_ugm3', 'sensor_community_1');
  const pm25 = latestMetricById(data, 'pm2_5_ugm3', 'sensor_community_1');

  const [pressureSeries, humiditySeries, rainSeries] = await Promise.all([
    fetchHistoryMetric('pressure_hpa', 'sensor_community_1'),
    fetchHistoryMetric('rh_pct', 'sensor_community_1'),
    fetchHistoryMetric('rain_mm_total', 'rain_node_mcu'),
  ]);

  const pressure6h = sampleBefore(pressureSeries, 6 * 3600);
  const humidity6h = sampleBefore(humiditySeries, 6 * 3600);
  const rain6h = sampleBefore(rainSeries, 6 * 3600);

  const pressureTrend = pressure && pressure6h ? Number(pressure.value) - Number(pressure6h.value) : null;
  const humidityTrend = humidity && humidity6h ? Number(humidity.value) - Number(humidity6h.value) : null;
  const rainDelta = rainTotal && rain6h ? Number(rainTotal.value) - Number(rain6h.value) : null;

  let label = 'Seguimiento local';
  let summary = 'Todavía no hay suficientes datos para una interpretación local de las próximas 24 horas.';
  let detail = 'La estación necesita más histórico o más variables activas para resumir la tendencia.';
  let confidence = 'baja';
  const signals = [];

  if (pressureTrend !== null) {{
    if (pressureTrend <= -3) signals.push('La presión ha bajado de forma clara en las últimas 6 horas.');
    else if (pressureTrend >= 3) signals.push('La presión ha subido de forma clara en las últimas 6 horas.');
    else signals.push('La presión se mantiene bastante estable.');
  }}
  if (humidityTrend !== null) {{
    if (humidityTrend >= 8) signals.push('La humedad está subiendo y el aire se siente más cargado.');
    else if (humidityTrend <= -8) signals.push('La humedad baja y el ambiente se seca algo más.');
  }}
  if (rainDelta !== null) {{
    if (rainDelta > 0.2) signals.push('Ha habido lluvia reciente según el pluviómetro.');
    else signals.push('No se observa lluvia reciente en el acumulado cercano.');
  }}
  if (wind && Number(wind.value) >= 8) signals.push('El viento ya se mueve con intensidad moderada o alta.');
  if (pm10 || pm25) {{
    const pmText = [pm10 ? `PM10 ${{formatMaybe(pm10.value, 1)}} ug/m3` : null, pm25 ? `PM2.5 ${{formatMaybe(pm25.value, 1)}} ug/m3` : null].filter(Boolean).join(', ');
    signals.push(`Las partículas se usan como contexto ambiental: ${{pmText}}.`);
  }}

  if (pressure && humidity) {{
    confidence = 'media';
    if (rainRate && Number(rainRate.value) > 0.1) {{
      label = 'Lluvia en curso';
      summary = 'Ahora mismo hay lluvia o indicios directos de precipitación en la estación.';
      detail = 'El pluviómetro ya marca precipitación reciente o activa. Si la presión sigue baja y la humedad alta, es razonable esperar continuidad a corto plazo.';
    }} else if (pressureTrend !== null && pressureTrend <= -2.5 && Number(humidity.value) >= 75) {{
      label = 'Inestabilidad probable';
      summary = 'En las próximas 24 horas podría aumentar la nubosidad y no se descarta lluvia débil.';
      detail = 'La presión cae y la humedad es alta. Esa combinación suele apuntar a un ambiente más inestable, aunque no garantiza precipitación.';
    }} else if (pressureTrend !== null && pressureTrend >= 2.5 && Number(humidity.value) <= 75) {{
      label = 'Mejora';
      summary = 'La tendencia general apunta a un tiempo más estable durante las próximas 24 horas.';
      detail = 'La presión sube y la humedad no está disparada. Eso suele acompañar una situación más tranquila y menos propensa a lluvia.';
    }} else {{
      label = 'Estable';
      summary = 'No se esperan cambios bruscos en las próximas 24 horas según la estación local.';
      detail = 'Las variables principales no muestran una señal fuerte de empeoramiento ni de mejora rápida.';
    }}
  }}

  return {{
    label,
    summary,
    detail,
    confidence,
    history: [],
    signals,
    variables: [
      {{ name: 'Presión atmosférica', value: pressure ? `${{formatMaybe(pressure.value, 1)}} hPa` : 'sin dato', description: 'Cuando baja con rapidez suele avisar de inestabilidad. Cuando sube, suele acompañar tiempo más estable.' }},
      {{ name: 'Humedad relativa', value: humidity ? `${{formatMaybe(humidity.value, 1)}} %` : 'sin dato', description: 'Una humedad alta indica aire cargado. Si además baja la presión, aumenta la posibilidad de nubosidad o lluvia.' }},
      {{ name: 'Temperatura en altura', value: temperature ? `${{formatMaybe(temperature.value, 1)}} C` : 'sin dato', description: 'Describe el ambiente en la parte alta de la estación. Ayuda a contextualizar, aunque por sí sola no predice cambios de tiempo.' }},
      {{ name: 'Lluvia reciente', value: rainTotal ? `${{formatMaybe(rainTotal.value, 2)}} mm acumulados` : 'sin dato', description: 'Nos ayuda a saber si ya hay un episodio húmedo en marcha o si la lluvia ha sido reciente.' }},
      {{ name: 'Viento', value: wind ? `${{formatMaybe(wind.value, 1)}} m/s` : 'sin dato', description: 'Si aumenta, puede reforzar la sensación de cambio y hacer el tiempo más incómodo.' }},
      {{ name: 'Luminosidad', value: light ? `${{formatMaybe(light.value, 1)}} lux` : 'sin dato', description: 'No predice por sí sola, pero ayuda a interpretar si el entorno está más abierto o más cubierto.' }},
      {{ name: 'Partículas en el aire', value: [pm10 ? `PM10 ${{formatMaybe(pm10.value, 1)}} ug/m3` : null, pm25 ? `PM2.5 ${{formatMaybe(pm25.value, 1)}} ug/m3` : null].filter(Boolean).join(', ') || 'sin dato', description: 'Las partículas no predicen la lluvia por sí solas, pero sí describen la calidad del aire y el contexto ambiental.' }},
    ],
    algorithm: [
      'Primero miramos la presión, porque es una de las señales más útiles para detectar cambios de estabilidad.',
      'Después comparamos humedad y lluvia reciente para saber si el ambiente está seco, estable o cargado.',
      'Añadimos viento y luminosidad para interpretar si el tiempo se siente más abierto, más cubierto o más incómodo.',
      'Las partículas PM10 y PM2.5 se incluyen como contexto de calidad del aire, no como predictor directo de lluvia.',
      'Con todo eso generamos una etiqueta simple: estable, mejora, inestabilidad probable o lluvia en curso.',
    ],
  }};
}}

async function renderForecast(data) {{
  const now = Date.now();
  if (!lastForecastData || (now - lastForecastFetch) > FORECAST_REFRESH_MS) {{
    lastForecastData = await buildForecast(data);
    lastForecastFetch = now;
  }}
  const forecast = lastForecastData;
  const repeatedSummary = String(forecast.label || '').trim() === String(forecast.summary || '').trim();
  const dash = document.getElementById('dashForecast');
  if (dash) {{
    dash.innerHTML = `
      <div class="forecast-top">
        <div class="forecast-icon">${{forecastIcon(forecast.label)}}</div>
        <div>
          <div class="forecast-title"><strong>${{dotHtml(Date.now()/1000, 600)}} Interpretación 24h</strong></div>
          <div><strong>${{forecast.label}}</strong></div>
        </div>
      </div>
      ${{repeatedSummary ? '' : `<div class="forecast-summary">${{forecast.summary}}</div>`}}
      <div class="forecast-signals"><small>${{(forecast.signals || []).slice(0, 3).join(' ')}}</small></div>
    `;
  }}
  const title = document.getElementById('forecastSummaryTitle');
  const summary = document.getElementById('forecastSummaryText');
  const detail = document.getElementById('forecastSummaryDetail');
  const confidence = document.getElementById('forecastConfidence');
  const variables = document.getElementById('forecastVariables');
  const algorithm = document.getElementById('forecastAlgorithm');
  const signals = document.getElementById('forecastSignals');
  const history = document.getElementById('forecastHistory');
  if (title) title.innerHTML = `<strong>${{forecast.label}}</strong>`;
  if (summary) summary.textContent = repeatedSummary ? '' : forecast.summary;
  if (detail) detail.textContent = forecast.detail;
  if (confidence) confidence.textContent = `Confianza orientativa: ${{forecast.confidence}}. Esta tarjeta resume una tendencia local, no un pronóstico oficial.`;
  if (variables) variables.innerHTML = forecast.variables.map(v => `<div style="margin-bottom:10px;"><strong>${{v.name}}</strong>: ${{v.value}}<br><span style="opacity:0.85;">${{v.description}}</span></div>`).join('');
  if (algorithm) algorithm.innerHTML = `<ol>${{forecast.algorithm.map(step => `<li style="margin-bottom:8px;">${{step}}</li>`).join('')}}</ol>`;
  if (signals) signals.innerHTML = forecast.signals.length ? `<ul>${{forecast.signals.map(item => `<li style="margin-bottom:8px;">${{item}}</li>`).join('')}}</ul>` : '<p>Sin señales suficientes todavía.</p>';
  if (history) {{
    const rows = Array.isArray(forecast.history) ? forecast.history : [];
    history.innerHTML = rows.length ? rows.map((item, index) => {{
      const when = item.ts ? new Date(item.ts * 1000).toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit'}}) : '--:--';
      const state = item.label || item.state_id || 'estado';
      const confidenceTag = item.confidence ? ` <span style="opacity:0.72;">${{item.confidence}}</span>` : '';
      const repeatTag = index > 0 && item.phrase === rows[index - 1].phrase ? ' <span style="opacity:0.72;">repetida</span>' : '';
      const summaryLine = item.summary ? `<div style="opacity:0.82; margin-top:4px;">${{item.summary}}</div>` : '';
      return `
        <div style="padding:10px 0; border-bottom:1px solid rgba(22,50,74,0.12);">
          <div><strong>${{when}}</strong> · ${{state}}${{confidenceTag}}${{repeatTag}}</div>
          <div style="margin-top:4px;">${{item.phrase || ''}}</div>
          ${{summaryLine}}
        </div>
      `;
    }}).join('') : '<p>Sin histórico suficiente todavía.</p>';
  }}
}}



    let soundState = null;
    let vaporAutomationState = null;
    let editingSoundRuleId = null;
    let editingSoundRuleRestoreEnabled = true;
    let soundRuleEditDirty = false;

    function metricOptionValue(item) {{
      return `${{item.device_id}}|||${{item.metric_id}}`;
    }}

    function readMetricSelection(selectId) {{
      const raw = document.getElementById(selectId)?.value || '';
      const [device_id, metric_id] = raw.split('|||');
      return {{ device_id: device_id || '', metric_id: metric_id || '' }};
    }}

    function readSelectedMetric() {{
      return readMetricSelection('soundMetricSelect');
    }}

    function collectStopRuleIds(rule) {{
      const values = [];
      for (const value of (rule?.stop_rule_ids || [])) {{
        const id = String(value || '').trim();
        if (id && !values.includes(id)) values.push(id);
      }}
      const legacy = String(rule?.stop_rule_id || '').trim();
      if (legacy && !values.includes(legacy)) values.push(legacy);
      return values.slice(0, 2);
    }}

    function formatStopRuleTargets(rule, rules) {{
      const ids = collectStopRuleIds(rule);
      if (!ids.length) return 'Ninguna';
      return ids.map(id => {{
        const match = rules.find(item => String(item.id) === String(id));
        return escapeHtml((match || {{}}).name || id);
      }}).join(' + ');
    }}

    function formatRuleRuntime(rule, payload) {{
      const runtime = (payload?.rule_runtime || {{}})[String(rule.id)] || {{}};
      const parts = [];
      if (runtime.playing) {{
        parts.push(`Audio ${{Number(runtime.playback_elapsed_sec || 0).toFixed(1)}} / ${{Number(runtime.playback_duration_sec || 0).toFixed(1)}} s`);
      }}
      if (!runtime.playing && Number(runtime.cooldown_remaining_sec || 0) > 0) {{
        parts.push(`Cooldown ${{Number(runtime.cooldown_remaining_sec || 0).toFixed(1)}} s`);
      }}
      if (runtime.preview) {{
        parts.push('Preview');
      }}
      return parts.join(' | ');
    }}

    function markSoundRuleEditDirty() {{
      soundRuleEditDirty = true;
    }}

    function buildInlineSoundRuleEditor(rule, payload) {{
      const rules = payload?.config?.rules || [];
      const sounds = payload?.sounds || [];
      const stopRuleIds = collectStopRuleIds(rule);
      const soundOptions = sounds.map(item => `<option value="${{item.name}}" ${{String(item.name) === String(rule.sound_file || '') ? 'selected' : ''}}>${{escapeHtml(item.label || item.name)}}</option>`).join('');
      const stopOptions1 = ['<option value="">Ninguna</option>'].concat(
        rules
          .filter(item => String(item.id) !== String(rule.id))
          .map(item => `<option value="${{item.id}}" ${{String(item.id) === String(stopRuleIds[0] || '') ? 'selected' : ''}}>${{escapeHtml(item.name || item.id)}}</option>`)
      ).join('');
      const stopOptions2 = ['<option value="">Ninguna</option>'].concat(
        rules
          .filter(item => String(item.id) !== String(rule.id))
          .map(item => `<option value="${{item.id}}" ${{String(item.id) === String(stopRuleIds[1] || '') ? 'selected' : ''}}>${{escapeHtml(item.name || item.id)}}</option>`)
      ).join('');
      return `
        <div style="margin-top:10px; padding:10px; background:rgba(22,50,74,0.04); border:1px solid rgba(22,50,74,0.10); border-radius:10px;">
          <div style="margin-bottom:10px; opacity:0.85;">Editando: ${{escapeHtml(rule.name || rule.id)}}</div>
          <div class="timelapse-controls">
            <label>Sonido:
              <select id="soundRuleEditFileSelect" onchange="markSoundRuleEditDirty()">
                ${{soundOptions}}
              </select>
            </label>
            <label>Bloquea regla:
              <select id="soundRuleEditStopRuleSelect" onchange="markSoundRuleEditDirty()">
                ${{stopOptions1}}
              </select>
            </label>
            <label>Bloquea regla 2:
              <select id="soundRuleEditStopRuleSelect2" onchange="markSoundRuleEditDirty()">
                ${{stopOptions2}}
              </select>
            </label>
            <button onclick="saveEditSoundRule()">Guardar cambios</button>
            <button onclick="cancelEditSoundRule()">Cancelar</button>
          </div>
        </div>
      `;
    }}

    function formatVaporRuleRuntime(rule, payload) {{
      const runtime = (payload?.engine?.rule_runtime || {{}})[String(rule.id)] || {{}};
      if (runtime.playing) {{
        return `Secuencia ${{Number(runtime.play_elapsed_sec || 0).toFixed(1)}} / ${{Number(runtime.play_duration_sec || 0).toFixed(1)}} s`;
      }}
      if (Number(runtime.cooldown_remaining_sec || 0) > 0) {{
        return `Cooldown ${{Number(runtime.cooldown_remaining_sec || 0).toFixed(1)}} s`;
      }}
      return 'Sin actividad reciente';
    }}

    function renderSoundState(payload) {{
      soundState = payload;
      const engine = payload.engine || {{}};
      const config = payload.config || {{}};
      const metrics = payload.metrics || [];
      const sounds = payload.sounds || [];
      const rules = config.rules || [];
      const modeLabels = {{
        once: 'Una vez',
        loop: 'Loop',
        interval: 'Intervalo',
      }};
      const activeRuleIds = new Set((engine.active_rule_ids || []).map(String));
      const suppressedRuleIds = new Set((engine.suppressed_rule_ids || []).map(String));
      const outputDevice = config.output_device || '';
      const outputLabel = outputDevice.includes('CD002') ? 'USB' : (outputDevice.includes('Headphones') ? 'Jack' : (outputDevice || '--'));
      const usbActive = outputDevice.includes('CD002');
      const jackActive = outputDevice.includes('Headphones');
      const volume = payload.volume || {{}};

      const status = document.getElementById('soundEngineStatus');
      if (status) {{
        status.textContent = `Estado: ${{engine.enabled ? 'activo' : 'parado'}} | salida: ${{outputLabel}} | reproduciendo: ${{engine.playing ? (engine.current_name || 'sí') : 'no'}}`;
      }}

      const volumeRange = document.getElementById('soundVolumeRange');
      const volumeStatus = document.getElementById('soundVolumeStatus');
      if (volumeRange && volume.percent != null) {{
        volumeRange.value = String(volume.percent);
      }}
      if (volumeStatus) {{
        volumeStatus.textContent = volume.percent != null
          ? `Volumen: ${{volume.percent}}% | salida activa: ${{outputLabel}}`
          : `Volumen no disponible | salida activa: ${{outputLabel}}`;
      }}

      const usbStatus = document.getElementById('soundOutputUsbStatus');
      if (usbStatus) {{
        usbStatus.classList.toggle('online', usbActive);
        usbStatus.classList.toggle('offline', !usbActive);
      }}

      const jackStatus = document.getElementById('soundOutputJackStatus');
      if (jackStatus) {{
        jackStatus.classList.toggle('online', jackActive);
        jackStatus.classList.toggle('offline', !jackActive);
      }}

      const metricSelect = document.getElementById('soundMetricSelect');
      if (metricSelect) {{
        const current = metricSelect.value;
        metricSelect.innerHTML = metrics.map(item => `<option value="${{metricOptionValue(item)}}">${{item.label}} (${{item.value}} ${{item.unit || ''}})</option>`).join('');
        if (current) metricSelect.value = current;
      }}

      const soundSelect = document.getElementById('soundFileSelect');
      if (soundSelect) {{
        const current = soundSelect.value;
        soundSelect.innerHTML = sounds.map(item => `<option value="${{item.name}}">${{item.label || item.name}}</option>`).join('');
        if (current) soundSelect.value = current;
      }}

      const stopRuleSelect = document.getElementById('soundStopRuleSelect');
      const stopRuleSelect2 = document.getElementById('soundStopRuleSelect2');
      const stopRuleOptions = ['<option value="">Ninguna</option>'].concat(
        rules.map(rule => `<option value="${{rule.id}}">${{escapeHtml(rule.name || rule.id)}}</option>`)
      ).join('');
      if (stopRuleSelect) {{
        const current = stopRuleSelect.value;
        stopRuleSelect.innerHTML = stopRuleOptions;
        if (current) stopRuleSelect.value = current;
      }}
      if (stopRuleSelect2) {{
        const current = stopRuleSelect2.value;
        stopRuleSelect2.innerHTML = stopRuleOptions;
        if (current) stopRuleSelect2.value = current;
      }}

      const filesList = document.getElementById('soundFilesList');
      if (filesList) {{
        filesList.innerHTML = sounds.length ? sounds.map(item => `
          <div style="margin-bottom:10px;">
            <strong>${{item.label || item.name}}</strong>${{item.size ? ` (${{item.size}} bytes)` : ''}}
            <button onclick="playSoundFile('${{item.name}}')">Play</button>
            ${{item.name === '__chirp__' ? '' : `<button onclick="playSoundFile('${{item.name}}', true)">Loop</button>`}}
            ${{item.name === '__chirp__' ? '' : `<button onclick="deleteSoundFile('${{item.name}}')">Borrar</button>`}}
          </div>
        `).join('') : '<p>Sin sonidos subidos todavía.</p>';
      }}

      const rulesList = document.getElementById('soundRulesList');
      if (rulesList) {{
        rulesList.innerHTML = rules.length ? rules.map(rule => `
          ${{(() => {{
            const ruleId = String(rule.id);
            const isActive = activeRuleIds.has(ruleId);
            const isSuppressed = suppressedRuleIds.has(ruleId);
            const isManualMuted = !!rule.muted;
            const dotClass = isActive ? 'online' : 'offline';
            const dotStyle = isSuppressed ? 'background:#f59e0b;' : (isManualMuted ? 'background:#ef4444;' : '');
            const stateText = isActive
              ? 'Disparando'
              : (isSuppressed ? 'Silenciada por otra regla' : (isManualMuted ? 'Muted manual' : (rule.enabled ? 'En espera' : 'Parada')));
            const muteLabel = isManualMuted ? 'Unmute' : 'Mute';
            const muteStyle = isManualMuted ? 'background:#ef4444; border-color:#ef4444; color:#fff;' : '';
            const suppressionTag = isSuppressed ? '<span style="display:inline-block; margin-left:8px; padding:2px 6px; border-radius:999px; background:#f59e0b; color:#fff; font-size:11px;">Muted temporal</span>' : '';
            const runtime = (payload?.rule_runtime || {{}})[ruleId] || {{}};
            const playbackPct = Math.max(0, Math.min(100, Number(runtime.playing ? (runtime.playback_progress_pct || 0) : 0)));
            const cooldownPct = Number(rule.cooldown_sec || 0) > 0
              ? Math.max(0, Math.min(100, 100 - (((runtime.cooldown_remaining_sec || 0) / Number(rule.cooldown_sec || 0)) * 100)))
              : 0;
            const progressPct = runtime.playing ? playbackPct : cooldownPct;
            const progressColor = runtime.playing ? '#22c55e' : ((runtime.cooldown_remaining_sec || 0) > 0 ? '#f59e0b' : 'transparent');
            return `
          <div style="margin-bottom:12px; padding-bottom:12px; border-bottom:1px solid rgba(22,50,74,0.08);">
            <strong><span class="dot ${{dotClass}}" style="${{dotStyle}}"></span>${{rule.name}}</strong><br>
            Sensor: ${{rule.device_id}} / ${{rule.metric_id}}<br>
            Sonido: ${{rule.sound_file}} | Modo: ${{modeLabels[rule.mode] || rule.mode}}<br>
            Estado: ${{stateText}}${{suppressionTag}}<br>
            Rango: ${{rule.min_value ?? '--'}} a ${{rule.max_value ?? '--'}} | Cambio mínimo: ${{rule.min_delta}} | Cooldown: ${{rule.cooldown_sec}}s | Volumen: ${{rule.volume_pct ?? 100}}%<br>
            <div style="margin:6px 0 8px 0; display:inline-block; min-width:220px; max-width:260px;">
              <div style="font-size:12px; opacity:0.86;">${{formatRuleRuntime(rule, payload) || 'Sin actividad reciente'}}</div>
              <div style="margin-top:4px; height:6px; background:rgba(22,50,74,0.12); border-radius:999px; overflow:hidden;">
                <div style="height:100%; width:${{progressPct}}%; background:${{progressColor}};"></div>
              </div>
            </div>
            Bloquea: ${{formatStopRuleTargets(rule, rules)}}<br>
            <button onclick="playSoundRule('${{rule.id}}')">Play</button>
            <button onclick="startEditSoundRule('${{rule.id}}')">${{String(editingSoundRuleId || '') === String(rule.id) ? 'Editando' : 'Editar'}}</button>
            <button onclick="toggleSoundRuleMute('${{rule.id}}')" style="${{muteStyle}}">${{muteLabel}}</button>
            <input type="range" min="0" max="100" step="1" value="${{Number(rule.volume_pct ?? 100)}}" oninput="previewSoundRuleVolume('${{rule.id}}', this.value)" onchange="setSoundRuleVolume('${{rule.id}}', this.value)" style="width:120px; vertical-align:middle; margin:0 8px;">
            <span id="soundRuleVolumeLabel_${{rule.id}}">${{Number(rule.volume_pct ?? 100)}}%</span>
            <button onclick="removeSoundRule('${{rule.id}}')">Borrar</button>
            ${{String(editingSoundRuleId || '') === String(rule.id) ? buildInlineSoundRuleEditor(rule, payload) : ''}}
          </div>
        `;
          }})()}}
        `).join('') : '<p>Sin reglas configuradas.</p>';
      }}
    }}

    async function loadSoundState() {{
      const res = await fetch('/api/sound/state');
      const data = await res.json();
      renderSoundState(data);
    }}

    async function toggleSoundGlobal(enabled) {{
      const form = new FormData();
      form.append('enabled', enabled ? 'true' : 'false');
      await fetch('/api/sound/global', {{ method: 'POST', body: form }});
      await loadSoundState();
    }}

    async function setSoundOutput(device) {{
      const form = new FormData();
      form.append('device', device);
      await fetch('/api/sound/output', {{ method: 'POST', body: form }});
      await loadSoundState();
    }}

    function onSoundVolumePreview(value) {{
      const status = document.getElementById('soundVolumeStatus');
      if (!status || !soundState) return;
      const config = soundState.config || {{}};
      const outputDevice = config.output_device || '';
      const outputLabel = outputDevice.includes('CD002') ? 'USB' : (outputDevice.includes('Headphones') ? 'Jack' : (outputDevice || '--'));
      status.textContent = `Volumen: ${{value}}% | salida activa: ${{outputLabel}}`;
    }}

    async function setSoundVolume(value) {{
      const form = new FormData();
      form.append('percent', String(value));
      await fetch('/api/sound/volume', {{ method: 'POST', body: form }});
      await loadSoundState();
    }}

    async function stopAllSound() {{
      await fetch('/api/sound/stop', {{ method: 'POST' }});
      await loadSoundState();
    }}

    async function playChirpTest() {{
      await fetch('/api/sound/test/chirp', {{ method: 'POST' }});
      await loadSoundState();
    }}

    async function playSoundFile(filename, loop = false) {{
      const form = new FormData();
      form.append('filename', filename);
      form.append('loop', loop ? 'true' : 'false');
      await fetch('/api/sound/test/file', {{ method: 'POST', body: form }});
      await loadSoundState();
    }}

    async function playSoundRule(ruleId) {{
      const form = new FormData();
      form.append('rule_id', ruleId);
      await fetch('/api/sound/test/rule', {{ method: 'POST', body: form }});
      await loadSoundState();
    }}

    async function deleteSoundFile(filename) {{
      if (!confirm(`¿Borrar sonido ${{filename}}? También se eliminarán sus reglas asociadas.`)) return;
      await fetch(`/api/sound/files/${{encodeURIComponent(filename)}}`, {{ method: 'DELETE' }});
      await loadSoundState();
    }}

    async function uploadSoundFile() {{
      const input = document.getElementById('soundUploadFile');
      const status = document.getElementById('soundUploadStatus');
      if (!input || !input.files || !input.files[0]) {{
        if (status) status.textContent = 'Selecciona primero un archivo.';
        return;
      }}
      const form = new FormData();
      form.append('file', input.files[0]);
      if (status) status.textContent = 'Subiendo...';
      const res = await fetch('/api/sound/upload', {{ method: 'POST', body: form }});
      const data = await res.json();
      if (status) status.textContent = data.ok ? `Subido: ${{data.file.original}}` : 'Error al subir';
      input.value = '';
      await loadSoundState();
    }}

    async function addSoundRule() {{
      if (!soundState) await loadSoundState();
      const metric = readSelectedMetric();
      const nextRules = [ ...(soundState?.config?.rules || []) ];
      const stopRuleIds = [
        document.getElementById('soundStopRuleSelect')?.value || '',
        document.getElementById('soundStopRuleSelect2')?.value || '',
      ].map(value => String(value || '').trim()).filter(Boolean).filter((value, index, arr) => arr.indexOf(value) === index).slice(0, 2);
      nextRules.push({{
        id: `rule_${{Date.now()}}`,
        name: document.getElementById('soundRuleName')?.value || 'Regla sonido',
        device_id: metric.device_id,
        metric_id: metric.metric_id,
        sound_file: document.getElementById('soundFileSelect')?.value || '',
        mode: document.getElementById('soundModeSelect')?.value || 'once',
        min_value: document.getElementById('soundMinValue')?.value || null,
        max_value: document.getElementById('soundMaxValue')?.value || null,
        min_delta: document.getElementById('soundMinDelta')?.value || 0,
        cooldown_sec: document.getElementById('soundCooldown')?.value || 10,
        volume_pct: document.getElementById('soundRuleVolume')?.value || 100,
        muted: !!document.getElementById('soundRuleMuted')?.checked,
        stop_rule_ids: stopRuleIds,
        stop_rule_id: stopRuleIds[0] || null,
        enabled: true,
      }});
      await persistSoundRules(nextRules);
    }}

    async function removeSoundRule(ruleId) {{
      await fetch(`/api/sound/rules/${{ruleId}}`, {{ method: 'DELETE' }});
      if (String(editingSoundRuleId || '') === String(ruleId)) {{
        editingSoundRuleId = null;
        editingSoundRuleRestoreEnabled = true;
        soundRuleEditDirty = false;
      }}
      await loadSoundState();
    }}

    async function startEditSoundRule(ruleId) {{
      if (!soundState) await loadSoundState();
      let rules = [ ...(soundState?.config?.rules || []) ];
      if (editingSoundRuleId && String(editingSoundRuleId) !== String(ruleId)) {{
        rules = rules.map(rule => String(rule.id) === String(editingSoundRuleId)
          ? {{ ...rule, enabled: editingSoundRuleRestoreEnabled }}
          : rule);
      }}
      const target = rules.find(rule => String(rule.id) === String(ruleId));
      if (!target) return;
      editingSoundRuleId = String(ruleId);
      editingSoundRuleRestoreEnabled = target.enabled !== false;
      soundRuleEditDirty = false;
      if (target.enabled !== false) {{
        const nextRules = rules.map(rule => String(rule.id) === String(ruleId)
          ? {{ ...rule, enabled: false }}
          : rule);
        await persistSoundRules(nextRules);
      }}
    }}

    async function cancelEditSoundRule() {{
      if (!editingSoundRuleId || !soundState) {{
        editingSoundRuleId = null;
        editingSoundRuleRestoreEnabled = true;
        soundRuleEditDirty = false;
        return;
      }}
      const rules = [ ...(soundState?.config?.rules || []) ];
      const nextRules = rules.map(rule => String(rule.id) === String(editingSoundRuleId)
        ? {{ ...rule, enabled: editingSoundRuleRestoreEnabled }}
        : rule);
      editingSoundRuleId = null;
      soundRuleEditDirty = false;
      await persistSoundRules(nextRules);
      editingSoundRuleRestoreEnabled = true;
    }}

    async function saveEditSoundRule() {{
      if (!editingSoundRuleId || !soundState) return;
      const fileSelect = document.getElementById('soundRuleEditFileSelect');
      const stopSelect1 = document.getElementById('soundRuleEditStopRuleSelect');
      const stopSelect2 = document.getElementById('soundRuleEditStopRuleSelect2');
      const stopRuleIds = [
        stopSelect1?.value || '',
        stopSelect2?.value || '',
      ].map(value => String(value || '').trim()).filter(Boolean).filter((value, index, arr) => arr.indexOf(value) === index).slice(0, 2);
      const rules = [ ...(soundState?.config?.rules || []) ];
      const nextRules = rules.map(rule => String(rule.id) === String(editingSoundRuleId)
        ? {{
            ...rule,
            sound_file: fileSelect?.value || rule.sound_file,
            stop_rule_ids: stopRuleIds,
            stop_rule_id: stopRuleIds[0] || null,
            enabled: editingSoundRuleRestoreEnabled,
          }}
        : rule);
      editingSoundRuleId = null;
      soundRuleEditDirty = false;
      await persistSoundRules(nextRules);
      editingSoundRuleRestoreEnabled = true;
    }}

    async function persistSoundRules(rules) {{
      await fetch('/api/sound/rules', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ rules }}),
      }});
      await loadSoundState();
    }}

    function previewSoundRuleVolume(ruleId, value) {{
      const label = document.getElementById(`soundRuleVolumeLabel_${{ruleId}}`);
      if (label) label.textContent = `${{value}}%`;
    }}

    async function setSoundRuleVolume(ruleId, value) {{
      if (!soundState) return;
      const rules = [ ...(soundState?.config?.rules || []) ];
      const nextRules = rules.map(rule => String(rule.id) === String(ruleId)
        ? {{ ...rule, volume_pct: Number(value) }}
        : rule);
      await persistSoundRules(nextRules);
    }}

    async function toggleSoundRuleMute(ruleId) {{
      if (!soundState) return;
      const rules = [ ...(soundState?.config?.rules || []) ];
      const nextRules = rules.map(rule => String(rule.id) === String(ruleId)
        ? {{ ...rule, muted: !rule.muted }}
        : rule);
      await persistSoundRules(nextRules);
    }}


    async function loadLatest() {{
      if (latestLoading) {{
        latestQueued = true;
        return;
      }}
      latestLoading = true;
      try {{
      const res = await fetch('/api/latest');
      const data = await res.json();
      renderDashWind(data);
      const rainTotal = getMetric(data, 'rain_mm_total') || getMetric(data, 'rain_mm');
      const rainRate = getMetric(data, 'rain_rate_mmh');
      const rainInterval = getMetric(data, 'rain_mm_interval');
      setCard(document.getElementById('dashRain'), 'Pluviómetro', [
        ['Lluvia acumulada', rainTotal ? `${{rainTotal.value}} ${{metricUnit(rainTotal.metric_id, rainTotal.unit)}}` : '--'],
        ['Intensidad', rainRate ? `${{rainRate.value}} ${{metricUnit('rain_rate_mmh', rainRate.unit)}}` : '--'],
        ['Intervalo', rainInterval ? `${{rainInterval.value}} ${{metricUnit('rain_mm_interval', rainInterval.unit)}}` : '--']
      ], max_ts(rainTotal, rainRate, rainInterval));

      const t = getMetric(data, 'temp_c');
      const rh = getMetric(data, 'rh_pct');
      const p = getMetric(data, 'pressure_hpa');
      const lux = getMetric(data, 'light_lux');
      const uvRaw = getMetric(data, 'uv_raw');
      const uvVoltage = getMetric(data, 'uv_voltage_v');
      const t2 = getMetric(data, 'temp_ground_c');
      const rh2 = getMetric(data, 'rh_ground_pct');
      const p2 = getMetric(data, 'pressure_ground_hpa');
      setCard(document.getElementById('dashBme'), 'Temp / Humedad / Presión', [
        ['Temperatura', t ? `${{Number(t.value).toFixed(2)}} ${{metricUnit('temp_c', t.unit)}}` : '--'],
        ['Humedad', rh ? `${{Number(rh.value).toFixed(2)}} ${{metricUnit('rh_pct', rh.unit)}}` : '--'],
        ['Presión', p ? `${{Number(p.value).toFixed(2)}} ${{metricUnit('pressure_hpa', p.unit)}}` : '--'],
        ['Temp suelo (sombra)', t2 ? `${{t2.value}} ${{metricUnit('temp_ground_c', t2.unit)}}` : '--'],
        ['Humedad suelo (sombra)', rh2 ? `${{rh2.value}} ${{metricUnit('rh_ground_pct', rh2.unit)}}` : '--'],
        ['Presión suelo (sombra)', p2 ? `${{p2.value}} ${{metricUnit('pressure_ground_hpa', p2.unit)}}` : '--']
      ], max_ts(t, rh, p, t2, rh2, p2));

      const lightTs = Math.max(
        latestTsByDevice(data, 'light_mcu'),
        lux ? lux.ts : 0,
        uvRaw ? uvRaw.ts : 0,
        uvVoltage ? uvVoltage.ts : 0
      );
      setCard(document.getElementById('dashLight'), 'Luminosidad / UV', [
        ['Nivel de luz', lux ? `${{lux.value}} ${{metricUnit('light_lux', lux.unit)}}` : '--'],
        ['UV Raw', uvRaw ? `${{uvRaw.value}} ${{metricUnit('uv_raw', uvRaw.unit)}}` : '--'],
        ['Voltaje UV', uvVoltage ? `${{uvVoltage.value}} ${{metricUnit('uv_voltage_v', uvVoltage.unit)}}` : '--']
      ], lightTs, STATUS_MAX_AGE_SEC.light_mcu);

      try {{
        const powerRes = await fetch('/api/pi/health');
        const power = await powerRes.json();
        setCard(document.getElementById('dashPower'), 'Alimentación Pi', [
          ['Undervoltage actual', power.current_undervoltage ? 'SI' : 'NO'],
          ['Undervoltage histórico', power.historical_undervoltage ? 'SI' : 'NO'],
          ['Throttling actual', power.current_throttled ? 'SI' : 'NO'],
          ['Throttling histórico', power.historical_throttled ? 'SI' : 'NO'],
          ['Frecuencia limitada', power.current_freq_capped ? 'SI' : 'NO'],
          ['Temperatura SoC', power.temperature_c != null ? `${{power.temperature_c.toFixed(1)}} C` : '--'],
          ['Voltaje core', power.core_voltage_v != null ? `${{power.core_voltage_v.toFixed(3)}} V` : '--'],
          ['Registro raw', power.throttled_raw || '--']
        ], power.ts || 0, 120);
      }} catch (_err) {{
        setCard(document.getElementById('dashPower'), 'Alimentación Pi', [
          ['Estado', 'NO DISPONIBLE']
        ], 0, 120);
      }}

      const pm10 = getMetric(data, 'pm10_ugm3');
      const pm25 = getMetric(data, 'pm2_5_ugm3');
      setCard(document.getElementById('dashAir'), 'PM / Calidad del aire', [
        ['PM10', pm10 ? `${{pm10.value}} ${{metricUnit('pm10_ugm3', pm10.unit)}}` : '--'],
        ['PM2.5', pm25 ? `${{pm25.value}} ${{metricUnit('pm2_5_ugm3', pm25.unit)}}` : '--']
      ], max_ts(pm10, pm25));

      renderDashGps(data);
      renderDashCam();
      await renderForecast(data);

      const bmeTs = Math.max(
        latestTsByDevice(data, 'sensor_community_1'),
        latestTsByDevice(data, 'bme280_local'),
        latestTsByDevice(data, 'bme280_ground'),
        latestTsByDevice(data, 'light_mcu')
      );
      setStatus('windStatus', latestTsByDevice(data, 'wind_esp8266'), STATUS_MAX_AGE_SEC.wind_esp8266);
      setStatus('rainStatus', latestTsByDevice(data, 'rain_node_mcu'), STATUS_MAX_AGE_SEC.rain_node_mcu);
      setStatus('bmeStatus', bmeTs, Math.max(STATUS_MAX_AGE_SEC.bme280_ground, STATUS_MAX_AGE_SEC.sensor_community_1, STATUS_MAX_AGE_SEC.light_mcu));
      const airTs = Math.max(latestTsByDevice(data, 'pm_sensor_1'), latestTsByDevice(data, 'sensor_community_1'));
      setStatus('airStatus', airTs, STATUS_MAX_AGE_SEC.sensor_community_1);
      setStatus('gpsStatus', latestTsByDevice(data, 'gps_usb_1'), STATUS_MAX_AGE_SEC.gps_usb_1);

      renderWind(data);
      renderSimpleTable('rainBody', ['rain_tips_total','rain_mm_total','rain_mm_interval','rain_rate_mmh','rain_last_tip_ms','rain_since_last_tip_ms','rain_mm'], data);
      renderSimpleTable('bmeBody', ['temp_c','rh_pct','pressure_hpa','light_lux','uv_raw','uv_voltage_v','temp_ground_c','rh_ground_pct','pressure_ground_hpa'], data);
      renderSimpleTable('airBody', ['pm10_ugm3','pm2_5_ugm3'], data);
      renderSimpleTable('gpsBody', ['gps_lat','gps_lon','gps_alt'], data);
      renderGPS(data);
      loadRainWindow();
      loadRain24h();
      }} finally {{
        latestLoading = false;
        if (latestQueued) {{
          latestQueued = false;
          loadLatest();
        }}
      }}
    }}

    function renderWind(data) {{
      const body = document.getElementById('windBody');
      body.innerHTML = '';
      const rows = Object.values(data).filter(v => v.device_id === 'wind_esp8266' && isWindMetric(v.metric_id));
      rows.sort((a,b) => a.metric_id.localeCompare(b.metric_id));
      const dirDegRow = rows.find(r => r.metric_id === 'wind_direction_deg');
      const dirDeg = dirDegRow ? Number(dirDegRow.value) : undefined;
      const calibratedDirDeg = applyWindCalibrationDeg(dirDeg);

      rows.forEach(v => {{
        const tr = document.createElement('tr');
        const label = metricLabel(v.metric_id);
        const unit = metricUnit(v.metric_id, v.unit);
        let value = v.value;
        if (v.metric_id === 'wind_direction_deg') {{
          value = calibratedDirDeg === null ? '--' : calibratedDirDeg.toFixed(0);
        }}
        if (v.metric_id === 'wind_direction_cardinal') {{
          value = windCardinal(calibratedDirDeg);
        }}
        tr.innerHTML = `<td>${{label}}</td><td>${{value}} ${{unit}}</td><td>${{new Date(v.ts*1000).toLocaleString()}}</td>`;
        body.appendChild(tr);
      }});

      const speed = rows.find(r => r.metric_id === 'wind_speed_ms');
      if (speed) {{
        windSeries.push({{ ts: speed.ts, value: speed.value }});
        while (windSeries.length > WIND_MAX_POINTS) windSeries.shift();
        drawWindChart();
      }}
    }}

    function drawWindChart() {{
      const canvas = document.getElementById('windChart');
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0,0,canvas.width,canvas.height);
      if (windSeries.length === 0) return;

      const xs = windSeries.map(p => p.ts);
      const ys = windSeries.map(p => p.value);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const minY = Math.min(...ys), maxY = Math.max(...ys);
      const pad = 10;

      ctx.beginPath();
      windSeries.forEach((p, i) => {{
        const x = pad + (p.ts - minX) / (maxX - minX || 1) * (canvas.width - pad*2);
        const y = canvas.height - pad - (p.value - minY) / (maxY - minY || 1) * (canvas.height - pad*2);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }});
      ctx.strokeStyle = '#1f7a8c';
      ctx.lineWidth = 2;
      ctx.stroke();
    }}

    async function loadRain24h(force = false) {{
      const now = Date.now();
      if (rain24hLoading) return;
      if (!force && (now - lastRain24hFetch) < RAIN_HISTORY_REFRESH_MS) return;
      rain24hLoading = true;
      try {{
        const res = await fetch('/api/history?metric=rain_mm_total');
        const data = await res.json();
        drawRainChart(data);
        lastRain24hFetch = Date.now();
      }} finally {{
        rain24hLoading = false;
      }}
    }}

    function drawRainChart(points) {{
      const canvas = document.getElementById('rainChart');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0,0,canvas.width,canvas.height);
      if (!points || points.length === 0) return;
      const now = Date.now()/1000;
      const day = points.filter(p => p.ts >= (now - 24*3600));
      if (day.length === 0) return;
      const xs = day.map(p => p.ts);
      const ys = day.map(p => p.value);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const minY = Math.min(...ys), maxY = Math.max(...ys);
      const pad = 10;
      ctx.beginPath();
      day.forEach((p, i) => {{
        const x = pad + (p.ts - minX) / (maxX - minX || 1) * (canvas.width - pad*2);
        const y = canvas.height - pad - (p.value - minY) / (maxY - minY || 1) * (canvas.height - pad*2);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }});
      ctx.strokeStyle = '#2a6';
      ctx.lineWidth = 2;
      ctx.stroke();
    }}

    function formatRainWindowTs(ts) {{
      if (!ts) return '--';
      return new Date(ts * 1000).toLocaleString();
    }}

    function formatRainWindowRemaining(targetTs) {{
      if (!targetTs) return '--';
      const remaining = Math.max(0, Math.round(targetTs - (Date.now() / 1000)));
      const hours = Math.floor(remaining / 3600);
      const minutes = Math.floor((remaining % 3600) / 60);
      return hours + 'h ' + String(minutes).padStart(2, '0') + 'm';
    }}

    function renderRainWindow(state) {{
      const amount = document.getElementById('rainWindowAmount');
      const tips = document.getElementById('rainWindowTips');
      const range = document.getElementById('rainWindowRange');
      const nextReset = document.getElementById('rainWindowNextReset');
      const mode = document.getElementById('rainWindowMode');
      if (amount) amount.textContent = state && state.window_total_mm != null ? `${{Number(state.window_total_mm).toFixed(2)}} mm` : '--';
      if (tips) tips.textContent = state && state.window_tips_total != null ? `Tics: ${{Number(state.window_tips_total).toFixed(0)}}` : 'Tics: --';
      if (range) range.textContent = state ? `Ventana: ${{formatRainWindowTs(state.window_start_ts)}} -> ${{formatRainWindowTs(state.window_end_ts)}}` : 'Ventana: --';
      if (nextReset) nextReset.textContent = state ? `Próximo reinicio: ${{formatRainWindowTs(state.next_reset_ts)}} (${{formatRainWindowRemaining(state.next_reset_ts)}})` : 'Próximo reinicio: --';
      if (mode) {{
        if (!state) mode.textContent = 'Modo: --';
        else if (state.window_source === 'manual') mode.textContent = 'Modo: anclaje manual cada 12h';
        else mode.textContent = 'Modo: ventana automática 00:00 / 12:00';
      }}
    }}

    async function loadRainWindow(force = false) {{
      const now = Date.now();
      if (rainWindowLoading) return;
      if (!force && (now - lastRainWindowFetch) < RAIN_WINDOW_REFRESH_MS) return;
      rainWindowLoading = true;
      try {{
        const res = await fetch('/api/rain/window');
        const data = await res.json();
        renderRainWindow(data);
        lastRainWindowFetch = Date.now();
      }} finally {{
        rainWindowLoading = false;
      }}
    }}

    async function resetRainWindow() {{
      const res = await fetch('/api/rain/window/reset', {{ method: 'POST' }});
      const data = await res.json();
      renderRainWindow(data);
      lastRainWindowFetch = Date.now();
      await loadRain24h(true);
    }}

    async function loadHistory() {{
      const metric = document.getElementById('metric').value.trim();
      if (!metric) return;
      const res = await fetch(`/api/history?metric=${{encodeURIComponent(metric)}}`);
      const data = await res.json();
      drawChart(data);
    }}

    function drawChart(points) {{
      const canvas = document.getElementById('chart');
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0,0,canvas.width,canvas.height);
      if (points.length === 0) return;
      const xs = points.map(p => p.ts);
      const ys = points.map(p => p.value);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const minY = Math.min(...ys), maxY = Math.max(...ys);
      const pad = 10;
      ctx.beginPath();
      points.forEach((p, i) => {{
        const x = pad + (p.ts - minX) / (maxX - minX || 1) * (canvas.width - pad*2);
        const y = canvas.height - pad - (p.value - minY) / (maxY - minY || 1) * (canvas.height - pad*2);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }});
      ctx.strokeStyle = '#2a6';
      ctx.lineWidth = 2;
      ctx.stroke();
    }}

    function startVideo() {{
      if (cameraStarted) return;
      cameraStarted = true;
      const video = document.getElementById('video');
      const snapshot = document.getElementById('cameraSnapshot');
      const fallbackText = document.getElementById('cameraFallbackText');
      const src = '{hls}';
      if (!video) return;
      video.preload = 'auto';
      let snapshotTimer = null;
      const refreshSnapshot = () => {{
        if (!snapshot) return;
        snapshot.src = `/hls/latest.jpg?ts=${{Date.now()}}`;
      }};
      const enableSnapshotFallback = () => {{
        if (snapshot) snapshot.style.display = 'block';
        if (fallbackText) fallbackText.style.display = 'block';
        refreshSnapshot();
        if (!snapshotTimer) snapshotTimer = setInterval(refreshSnapshot, 1000);
      }};
      const disableSnapshotFallback = () => {{
        if (snapshot) snapshot.style.display = 'none';
        if (fallbackText) fallbackText.style.display = 'none';
        if (snapshotTimer) {{
          clearInterval(snapshotTimer);
          snapshotTimer = null;
        }}
      }};
      const tryPlay = () => {{
        const p = video.play();
        if (p && typeof p.catch === 'function') {{
          p.catch(() => {{}});
        }}
      }};
      const armFallback = () => setTimeout(() => {{
        if (video.readyState < 2 || video.currentTime < 0.1) enableSnapshotFallback();
      }}, 6000);
      video.addEventListener('playing', disableSnapshotFallback);
      video.addEventListener('loadeddata', disableSnapshotFallback);
      enableSnapshotFallback();
      if (video.canPlayType('application/vnd.apple.mpegurl')) {{
        video.src = src;
        video.addEventListener('loadedmetadata', tryPlay, {{ once: true }});
        tryPlay();
        armFallback();
      }} else if (window.Hls && Hls.isSupported()) {{
        const hls = new Hls({{
          maxLiveSyncPlaybackRate: 1.5,
        }});
        hls.loadSource(src);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {{
          tryPlay();
          armFallback();
        }});
        hls.on(Hls.Events.ERROR, (_, data) => {{
          if (!data || !data.fatal) return;
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {{
            hls.startLoad();
            enableSnapshotFallback();
          }} else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {{
            hls.recoverMediaError();
            enableSnapshotFallback();
          }} else {{
            hls.destroy();
            enableSnapshotFallback();
          }}
        }});
      }} else {{
        enableSnapshotFallback();
      }}
    }}

    let timelapseOffset = 0;
    const TIMELAPSE_LIMIT = 30;
    let timelapseLoading = false;

    async function loadTimelapseStatus() {{
      const res = await fetch('/api/timelapse/status');
      const data = await res.json();
      const status = document.getElementById('timelapseStatus');
      if (status) status.textContent = `Estado: ${{data.running ? 'En marcha' : 'Parado'}}`;
      const input = document.getElementById('timelapseInterval');
      if (input && data.interval_sec) input.value = data.interval_sec;
    }}

    async function startTimelapse() {{
      const input = document.getElementById('timelapseInterval');
      const val = input ? input.value : 60;
      await fetch(`/api/timelapse/start?interval_sec=${{encodeURIComponent(val)}}`, {{ method: 'POST' }});
      loadTimelapseStatus();
    }}

    async function stopTimelapse() {{
      await fetch('/api/timelapse/stop', {{ method: 'POST' }});
      loadTimelapseStatus();
    }}

    async function loadMoreTimelapse() {{
      if (timelapseLoading) return;
      timelapseLoading = true;
      const res = await fetch(`/api/timelapse/list?offset=${{timelapseOffset}}&limit=${{TIMELAPSE_LIMIT}}`);
      const data = await res.json();
      const gallery = document.getElementById('timelapseGallery');
      if (gallery && data.items) {{
        data.items.forEach(f => {{
          const img = document.createElement('img');
          img.loading = 'lazy';
          img.src = `/timelapse/${{f}}`;
          gallery.appendChild(img);
        }});
        timelapseOffset += data.items.length;
      }}
      timelapseLoading = false;
    }}

    async function createGif() {{
      const status = document.getElementById('gifStatus');
      if (status) status.textContent = 'Generando GIF...';
      const input = document.getElementById('gifInterval');
      const val = input ? input.value : 1;
      const res = await fetch(`/api/timelapse/gif?interval_sec=${{encodeURIComponent(val)}}`, {{ method: 'POST' }});
      const data = await res.json();
      if (data.ok && data.filename) {{
        const link = document.getElementById('gifLink');
        if (link) {{
          link.href = `/timelapse/${{data.filename}}`;
          link.textContent = data.filename;
        }}
        const img = document.getElementById('gifPreview');
        if (img) img.src = `/timelapse/${{data.filename}}?ts=${{Date.now()}}`;
        if (status) status.textContent = 'GIF creado';
      }} else {{
        if (status) status.textContent = 'No hay imagenes para el GIF';
      }}
    }}

    async function clearTimelapse() {{
      if (!confirm('¿Seguro que quieres borrar toda la galería?')) return;
      const status = document.getElementById('gifStatus');
      if (status) status.textContent = 'Borrando...';
      await fetch('/api/timelapse/clear', {{ method: 'POST' }});
      const gallery = document.getElementById('timelapseGallery');
      if (gallery) gallery.innerHTML = '';
      timelapseOffset = 0;
      if (status) status.textContent = 'Galería vacía';
      const link = document.getElementById('gifLink');
      if (link) link.textContent = '';
      const img = document.getElementById('gifPreview');
      if (img) img.removeAttribute('src');
    }}

    function setupTimelapseScroll() {{
      const gallery = document.getElementById('timelapseGallery');
      if (!gallery || timelapseScrollBound) return;
      timelapseScrollBound = true;
      gallery.addEventListener('scroll', () => {{
        if (gallery.scrollTop + gallery.clientHeight >= gallery.scrollHeight - 50) {{
          loadMoreTimelapse();
        }}
      }});
    }}

    const evt = new EventSource('/api/stream');
    evt.onmessage = () => loadLatest();
    renderGPS({{}});
    renderDashGps({{}});
    renderVaporState({{ configured: false, online: false, relay_on: false }});
    renderFanState({{ configured: false, online: false, relay_on: false }});
    renderSmokeState({{ configured: false, online: false, relay_on: false }});
    renderVaporSequenceState({{ recording: false, playing: false, step_count: 0, duration_sec: 0, initial: {{ vapor: false, fan: false, smoke: false }}, steps: [], sequences: [] }});
    renderVaporAutomationState({{ config: {{ rules: [] }}, metrics: [], engine: {{ running: false, active_rule_ids: [] }}, sequence: {{ step_count: 0 }} }});
    renderFxState({{ config: {{ text_color_mode: 'auto' }}, presets: [] }});
    renderExportState({{ running: false, files: [] }});
    renderWindCalibrationState(null);
    loadLatest();
    loadVaporState();
    loadFanState();
    loadSmokeState();
    loadVaporSequenceState();
    loadVaporAutomationState();
    loadFxState();
    loadExportState();
    loadWindCalibration();
    const activePanel = document.querySelector('.panel.active');
    if (activePanel) {{
      initializeTab(activePanel.id);
    }}
    setInterval(loadVaporState, 5000);
    setInterval(loadFanState, 5000);
    setInterval(loadVaporSequenceState, 1000);
    setInterval(loadVaporAutomationState, 1000);
    setInterval(loadSoundState, 1000);
    setInterval(loadFxState, 10000);
    setInterval(loadExportState, 5000);
    setInterval(loadWindCalibration, 10000);
  </script>
</body>
</html>
""")




