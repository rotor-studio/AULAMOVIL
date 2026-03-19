import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import wave
import math
import struct
from array import array
from datetime import datetime, timezone

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
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
USB_AUDIO_DEVICE = "plughw:CARD=CD002,DEV=0"

timelapse_lock = threading.Lock()
timelapse_thread = None
timelapse_stop = threading.Event()
timelapse_interval = 60

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



os.makedirs(SOUND_DIR, exist_ok=True)


def default_sound_config():
    return {
        "enabled": False,
        "rules": [],
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
        data.setdefault("rules", [])
        return data
    except Exception:
        return default_sound_config()


def save_sound_config(config):
    with open(SOUND_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


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
        self.rule_state = {}
        self.global_enabled = False
        self.sample_rate = 22050
        self.channels = 2
        self.chunk_frames = 2048
        self.voices = []
        self.decoded_cache = {}

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
            return {
                "enabled": self.global_enabled,
                "playing": bool(self.voices),
                "current_rule_id": ",".join(sorted({str(voice.get("rule_id") or "") for voice in self.voices if voice.get("rule_id")})) or None,
                "current_name": ", ".join(names) if names else None,
                "current_loop": any(voice.get("loop") for voice in self.voices),
                "voice_count": len(self.voices),
            }

    def set_enabled(self, enabled):
        with self.lock:
            self.global_enabled = bool(enabled)
            if not self.global_enabled:
                self.voices.clear()
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

    def _ensure_player_locked(self):
        if self.player and self.player.poll() is None:
            return
        self._stop_player_locked()
        self.player = subprocess.Popen(
            [
                "aplay",
                "-q",
                "-D",
                USB_AUDIO_DEVICE,
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

    def stop_sound(self):
        with self.lock:
            self.voices.clear()

    def _queue_voice_locked(self, samples, loop, name, rule_id):
        self.voices.append({
            "samples": samples,
            "position": 0,
            "loop": loop,
            "name": name,
            "rule_id": rule_id,
        })

    def play_file(self, filename, loop=False, rule_id=None):
        if filename == "__chirp__":
            self.play_chirp(rule_id=rule_id)
            return
        path = os.path.join(SOUND_DIR, filename)
        samples = self.decode_file(path)
        with self.lock:
            self._queue_voice_locked(samples, loop=loop, name=filename, rule_id=rule_id)

    def play_chirp(self, rule_id="test"):
        samples = build_chirp_pcm()
        with self.lock:
            self._queue_voice_locked(samples, loop=False, name="chirp", rule_id=rule_id)

    def audio_loop(self):
        silence = array("h", [0] * (self.chunk_frames * self.channels))
        while not self.stop_event.wait(0.01):
            with self.lock:
                if not self.global_enabled:
                    self._stop_player_locked()
                    continue
                if not self.voices:
                    if self.player and self.player.poll() is None and self.player.stdin:
                        try:
                            self.player.stdin.write(silence.tobytes())
                            self.player.stdin.flush()
                        except Exception:
                            self._stop_player_locked()
                    else:
                        self._ensure_player_locked()
                    continue

                self._ensure_player_locked()
                mix = [0] * (self.chunk_frames * self.channels)
                next_voices = []
                for voice in self.voices:
                    samples = voice["samples"]
                    pos = voice["position"]
                    for frame in range(self.chunk_frames):
                        if pos >= len(samples):
                            if voice["loop"]:
                                pos = 0
                            else:
                                break
                        for ch in range(self.channels):
                            if pos + ch < len(samples):
                                mix[frame * self.channels + ch] += samples[pos + ch]
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
                try:
                    if self.player and self.player.stdin:
                        self.player.stdin.write(out.tobytes())
                        self.player.stdin.flush()
                except Exception:
                    self._stop_player_locked()

    def run(self):
        while not self.stop_event.wait(1.0):
            config = load_sound_config()
            self.set_enabled(config.get("enabled", False))
            if not self.global_enabled:
                continue
            latest = get_latest_map()
            rules = config.get("rules", [])
            for rule in rules:
                if not rule.get("enabled", True):
                    self.stop_rule_sound(rule.get("id"))
                    continue
                metric_id = rule.get("metric_id")
                device_id = rule.get("device_id")
                sound_file = rule.get("sound_file")
                if not metric_id or not sound_file:
                    continue
                item = get_metric(latest, metric_id, device_id)
                if not item:
                    continue
                value = float(item["value"])
                state = self.rule_state.setdefault(rule["id"], {
                    "last_value": None,
                    "active": False,
                    "last_trigger_ts": 0.0,
                })
                min_value = rule.get("min_value")
                max_value = rule.get("max_value")
                min_delta = float(rule.get("min_delta") or 0.0)
                cooldown = float(rule.get("cooldown_sec") or 10.0)
                in_range = True
                if min_value not in (None, ""):
                    in_range = in_range and value >= float(min_value)
                if max_value not in (None, ""):
                    in_range = in_range and value <= float(max_value)
                delta = None if state["last_value"] is None else abs(value - state["last_value"])
                changed = state["last_value"] is None or delta >= min_delta
                now = time.time()
                mode = rule.get("mode", "once")
                is_current = self.is_rule_playing(rule["id"])
                is_chirp = sound_file == "__chirp__"
                try_trigger = in_range and changed and (now - state["last_trigger_ts"] >= cooldown)

                if mode == "loop" and not is_chirp:
                    if in_range and not is_current:
                        try:
                            self.play_file(sound_file, loop=True, rule_id=rule["id"])
                            state["last_trigger_ts"] = now
                            state["active"] = True
                        except Exception:
                            pass
                    elif not in_range and is_current:
                        self.stop_rule_sound(rule["id"])
                        state["active"] = False
                    elif not in_range:
                        state["active"] = False
                else:
                    if try_trigger and not state["active"]:
                        try:
                            self.play_file(sound_file, loop=False, rule_id=rule["id"])
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
    pressure = get_metric(latest, "pressure_hpa", "sensor_community_1")
    humidity = get_metric(latest, "rh_pct", "sensor_community_1")
    temperature = get_metric(latest, "temp_c", "sensor_community_1")
    wind = get_metric(latest, "wind_speed_ms", "wind_esp8266")
    rain_rate = get_metric(latest, "rain_rate_mmh", "rain_node_mcu")
    rain_total = get_metric(latest, "rain_mm_total", "rain_node_mcu") or get_metric(latest, "rain_mm", "rain_node_mcu")
    light = get_metric(latest, "light_lux", "wind_esp8266")
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

    headline = label
    line1 = summary
    line2 = air_label
    if len(line1) > 64:
        line1 = line1[:61] + "..."
    if len(line2) > 64:
        line2 = line2[:61] + "..."

    last_ts = max([
        item["ts"] for item in [pressure, humidity, temperature, wind, rain_rate, rain_total, light, pm10, pm25] if item
    ], default=0)

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
        "display": {
            "headline": headline,
            "line1": line1,
            "line2": line2,
            "brightness": 64,
            "effect": "solid",
        },
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



@app.get("/api/sign/latest")
def api_sign_latest():
    return compute_forecast_payload()


@app.get("/api/sound/state")
def api_sound_state():
    config = load_sound_config()
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
    sounds = [{"name": "__chirp__", "size": 0, "label": "Chirp interno"}] + list_sound_files()
    return {
        "config": config,
        "sounds": sounds,
        "engine": sound_engine.status(),
        "metrics": metrics,
    }


@app.post("/api/sound/global")
def api_sound_global(enabled: bool = Form(...)):
    config = load_sound_config()
    config["enabled"] = bool(enabled)
    save_sound_config(config)
    sound_engine.set_enabled(enabled)
    return {"ok": True, "config": config, "engine": sound_engine.status()}


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


@app.post("/api/sound/upload")
async def api_sound_upload(file: UploadFile = File(...)):
    os.makedirs(SOUND_DIR, exist_ok=True)
    original = file.filename or "sound.wav"
    ext = os.path.splitext(original)[1].lower()
    if ext not in {".wav", ".mp3", ".ogg"}:
        raise HTTPException(status_code=400, detail="Formato no soportado. Usa wav, mp3 u ogg.")
    safe_name = f"{uuid.uuid4().hex}{ext}"
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
    normalized = []
    for raw in rules:
        rid = raw.get("id") or uuid.uuid4().hex
        normalized.append({
            "id": rid,
            "name": str(raw.get("name") or rid),
            "device_id": raw.get("device_id") or "",
            "metric_id": raw.get("metric_id") or "",
            "sound_file": raw.get("sound_file") or "",
            "mode": raw.get("mode") or "once",
            "min_value": raw.get("min_value"),
            "max_value": raw.get("max_value"),
            "min_delta": float(raw.get("min_delta") or 0.0),
            "cooldown_sec": float(raw.get("cooldown_sec") or 10.0),
            "enabled": bool(raw.get("enabled", True)),
        })
    config = load_sound_config()
    config["rules"] = normalized
    save_sound_config(config)
    return {"ok": True, "config": config}


@app.delete("/api/sound/rules/{rule_id}")
def api_sound_delete_rule(rule_id: str):
    config = load_sound_config()
    config["rules"] = [rule for rule in config.get("rules", []) if rule.get("id") != rule_id]
    save_sound_config(config)
    return {"ok": True, "config": config}


@app.get("/api/stream")
def api_stream():
    async def event_stream():
        last_hash = None
        while True:
            payload = json.dumps(list(get_latest_map().values()), separators=(",", ":"))
            h = hash(payload)
            if h != last_hash:
                last_hash = h
                yield f"data: {payload}\n\n"
            await asyncio.sleep(SSE_INTERVAL)

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


@app.on_event("startup")
def startup():
    sound_engine.start()


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
  <title>AULA MOVIL - ROTOR STUDIO</title>
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
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
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
    .card-wide {{ grid-column: 1 / -1; }}
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
  <h1>AULA MOVIL - ROTOR STUDIO</h1>
  <div class=\"tabs\">
    <div class=\"tab active\" data-tab=\"dashboard\">Dashboard</div>
    <div class=\"tab\" data-tab=\"wind\">Viento</div>
    <div class=\"tab\" data-tab=\"rain\">Pluviómetro</div>
    <div class=\"tab\" data-tab=\"bme\">Temp/Humedad/Presión/Lux</div>
    <div class=\"tab\" data-tab=\"air\">PM y Aire</div>
    <div class=\"tab\" data-tab=\"gps\">Posición</div>
    <div class=\"tab\" data-tab=\"camera\">Cámara</div>
    <div class=\"tab\" data-tab=\"sound\">Sonido</div>
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
      <div id=\"dashForecast\" class=\"card card-wide forecast-card\"></div>
    </div>
  </div>

  <div id=\"wind\" class=\"panel\">
    <h2>Viento (ESP8266)</h2>
    <div class=\"status-line\">Estado: <span id=\"windStatus\" class=\"dot offline\"></span><span id=\"windStatusText\">Offline</span></div>
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
    <table>
      <thead><tr><th>Métrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"rainBody\"></tbody>
    </table>
    <h3>Acumulado (24h)</h3>
    <canvas id=\"rainChart\" width=\"700\" height=\"220\"></canvas>
  </div>

  <div id=\"bme\" class=\"panel\">
    <h2>Temperatura / Humedad / Presión / Lux</h2>
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

  <div id=\"sound\" class=\"panel\">
    <h2>Sonido</h2>
    <div class=\"card\">
      <div class=\"timelapse-actions\">
        <button onclick=\"toggleSoundGlobal(true)\">Play General</button>
        <button onclick=\"toggleSoundGlobal(false)\">Stop General</button>
        <button onclick=\"stopAllSound()\">Cortar Sonido</button>
        <button onclick=\"playChirpTest()\">Probar Chirp</button>
      </div>
      <div id=\"soundEngineStatus\" style=\"margin-top:8px; opacity:0.85;\">Estado: --</div>
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
          </select>
        </label>
        <label>Min valor: <input id=\"soundMinValue\" type=\"number\" step=\"any\" /></label>
        <label>Max valor: <input id=\"soundMaxValue\" type=\"number\" step=\"any\" /></label>
        <label>Cambio mínimo: <input id=\"soundMinDelta\" type=\"number\" step=\"any\" value=\"1\" /></label>
        <label>Cooldown (s): <input id=\"soundCooldown\" type=\"number\" step=\"1\" value=\"10\" /></label>
        <button onclick=\"addSoundRule()\">Añadir regla</button>
      </div>
      <div style=\"margin-top:8px; opacity:0.85;\">Una regla se dispara cuando el sensor entra en rango y cambia al menos el valor indicado.</div>
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

  <div id=\"camera\" class=\"panel\">
    <h2>Cámara (Live)</h2>
    <div class=\"status-line\">Estado: <span id=\"cameraStatus\" class=\"dot offline\"></span><span id=\"cameraStatusText\">Offline</span></div>
    <video id=\"video\" controls autoplay muted playsinline></video>
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
      sensor_community_1: 300,
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
      const dirStr = (dir === undefined) ? '--' : Number(dir).toFixed(0);
      const cardinal = windCardinal(Number(dir));
      const unitSpeed = metricUnit('wind_speed_ms', 'm/s');

      const card = document.createElement('div');
      card.className = 'card wind-card';
      const deg = (dir === undefined || isNaN(dir)) ? 0 : Number(dir);
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
    }}

    document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => setTab(t.dataset.tab)));

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
      const key = Object.keys(data).find(k => data[k].metric_id === id);
      return key ? data[key] : null;
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

    function buildSummaryCard(title, rows, ts) {{
      const card = document.createElement('div');
      card.className = 'card';
      const timeText = ts ? new Date(ts*1000).toLocaleString() : '';
      const rowsHtml = rows.map(r => `<div>${{r[0]}}: ${{r[1]}}</div>`).join('');
      card.innerHTML = `<strong>${{title}}</strong>${{rowsHtml}}<div><small>${{timeText}}</small></div>`;
      return card;
    }}

    function setCard(el, title, rows, ts) {{
      if (!el) return;
      const timeText = ts ? new Date(ts*1000).toLocaleString() : '';
      const rowsHtml = rows.map(r => `<div>${{r[0]}}: ${{r[1]}}</div>`).join('');
      el.innerHTML = `<strong>${{dotHtml(ts)}} ${{title}}</strong>${{rowsHtml}}<div><small>${{timeText}}</small></div>`;
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
      const dirStr = (dir === undefined) ? '--' : Number(dir).toFixed(0);
      const cardinal = windCardinal(Number(dir));
      const unitSpeed = metricUnit('wind_speed_ms', 'm/s');
      const deg = (dir === undefined || isNaN(dir)) ? 0 : Number(dir);
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
const FORECAST_REFRESH_MS = 120000;

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
  const pressure = latestMetricById(data, 'pressure_hpa', 'sensor_community_1');
  const humidity = latestMetricById(data, 'rh_pct', 'sensor_community_1');
  const temperature = latestMetricById(data, 'temp_c', 'sensor_community_1');
  const wind = latestMetricById(data, 'wind_speed_ms', 'wind_esp8266');
  const rainRate = latestMetricById(data, 'rain_rate_mmh', 'rain_node_mcu');
  const rainTotal = latestMetricById(data, 'rain_mm_total', 'rain_node_mcu');
  const light = latestMetricById(data, 'light_lux', 'wind_esp8266');
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
      <div class="forecast-summary">${{forecast.summary}}</div>
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
  if (title) title.innerHTML = `<strong>${{forecast.label}}</strong>`;
  if (summary) summary.textContent = forecast.summary;
  if (detail) detail.textContent = forecast.detail;
  if (confidence) confidence.textContent = `Confianza orientativa: ${{forecast.confidence}}. Esta tarjeta resume una tendencia local, no un pronóstico oficial.`;
  if (variables) variables.innerHTML = forecast.variables.map(v => `<div style="margin-bottom:10px;"><strong>${{v.name}}</strong>: ${{v.value}}<br><span style="opacity:0.85;">${{v.description}}</span></div>`).join('');
  if (algorithm) algorithm.innerHTML = `<ol>${{forecast.algorithm.map(step => `<li style="margin-bottom:8px;">${{step}}</li>`).join('')}}</ol>`;
  if (signals) signals.innerHTML = forecast.signals.length ? `<ul>${{forecast.signals.map(item => `<li style="margin-bottom:8px;">${{item}}</li>`).join('')}}</ul>` : '<p>Sin señales suficientes todavía.</p>';
}}



    let soundState = null;

    function metricOptionValue(item) {{
      return `${{item.device_id}}|||${{item.metric_id}}`;
    }}

    function readSelectedMetric() {{
      const raw = document.getElementById('soundMetricSelect')?.value || '';
      const [device_id, metric_id] = raw.split('|||');
      return {{ device_id: device_id || '', metric_id: metric_id || '' }};
    }}

    function renderSoundState(payload) {{
      soundState = payload;
      const engine = payload.engine || {{}};
      const config = payload.config || {{}};
      const metrics = payload.metrics || [];
      const sounds = payload.sounds || [];
      const rules = config.rules || [];

      const status = document.getElementById('soundEngineStatus');
      if (status) {{
        status.textContent = `Estado: ${{engine.enabled ? 'activo' : 'parado'}} | reproduciendo: ${{engine.playing ? (engine.current_name || 'sí') : 'no'}}`;
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
          <div style="margin-bottom:12px; padding-bottom:12px; border-bottom:1px solid rgba(22,50,74,0.08);">
            <strong>${{rule.name}}</strong><br>
            Sensor: ${{rule.device_id}} / ${{rule.metric_id}}<br>
            Sonido: ${{rule.sound_file}} | Modo: ${{rule.mode}}<br>
            Rango: ${{rule.min_value ?? '--'}} a ${{rule.max_value ?? '--'}} | Cambio mínimo: ${{rule.min_delta}} | Cooldown: ${{rule.cooldown_sec}}s | ${{rule.enabled ? 'Activa' : 'Parada'}}<br>
            <button onclick="removeSoundRule('${{rule.id}}')">Borrar</button>
          </div>
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
        enabled: true,
      }});
      await fetch('/api/sound/rules', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ rules: nextRules }}),
      }});
      await loadSoundState();
    }}

    async function removeSoundRule(ruleId) {{
      await fetch(`/api/sound/rules/${{ruleId}}`, {{ method: 'DELETE' }});
      await loadSoundState();
    }}


    async function loadLatest() {{
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
      const t2 = getMetric(data, 'temp_ground_c');
      const rh2 = getMetric(data, 'rh_ground_pct');
      setCard(document.getElementById('dashBme'), 'Temp / Humedad / Presión', [
        ['Temperatura', t ? `${{t.value}} ${{metricUnit('temp_c', t.unit)}}` : '--'],
        ['Humedad', rh ? `${{rh.value}} ${{metricUnit('rh_pct', rh.unit)}}` : '--'],
        ['Presión', p ? `${{p.value}} ${{metricUnit('pressure_hpa', p.unit)}}` : '--'],
        ['Temp suelo (sombra)', t2 ? `${{t2.value}} ${{metricUnit('temp_ground_c', t2.unit)}}` : '--'],
        ['Humedad suelo (sombra)', rh2 ? `${{rh2.value}} ${{metricUnit('rh_ground_pct', rh2.unit)}}` : '--']
      ], max_ts(t, rh, p, t2, rh2));

      setCard(document.getElementById('dashLight'), 'Luminosidad', [
        ['Nivel de luz', lux ? `${{lux.value}} ${{metricUnit('light_lux', lux.unit)}}` : '--']
      ], max_ts(lux));

      const pm10 = getMetric(data, 'pm10_ugm3');
      const pm25 = getMetric(data, 'pm2_5_ugm3');
      setCard(document.getElementById('dashAir'), 'PM / Calidad del aire', [
        ['PM10', pm10 ? `${{pm10.value}} ${{metricUnit('pm10_ugm3', pm10.unit)}}` : '--'],
        ['PM2.5', pm25 ? `${{pm25.value}} ${{metricUnit('pm2_5_ugm3', pm25.unit)}}` : '--']
      ], max_ts(pm10, pm25));

      renderDashGps(data);
      renderDashCam();
      await renderForecast(data);

      const bmeTs = Math.max(latestTsByDevice(data, 'bme280_local'), latestTsByDevice(data, 'bme280_ground'), getMetric(data, 'light_lux') ? getMetric(data, 'light_lux').ts : 0);
      setStatus('windStatus', latestTsByDevice(data, 'wind_esp8266'), STATUS_MAX_AGE_SEC.wind_esp8266);
      setStatus('rainStatus', latestTsByDevice(data, 'rain_node_mcu'), STATUS_MAX_AGE_SEC.rain_node_mcu);
      setStatus('bmeStatus', bmeTs, Math.max(STATUS_MAX_AGE_SEC.bme280_ground, STATUS_MAX_AGE_SEC.sensor_community_1));
      const airTs = Math.max(latestTsByDevice(data, 'pm_sensor_1'), latestTsByDevice(data, 'sensor_community_1'));
      setStatus('airStatus', airTs, STATUS_MAX_AGE_SEC.sensor_community_1);
      setStatus('gpsStatus', latestTsByDevice(data, 'gps_usb_1'), STATUS_MAX_AGE_SEC.gps_usb_1);

      renderWind(data);
      renderSimpleTable('rainBody', ['rain_tips_total','rain_mm_total','rain_mm_interval','rain_rate_mmh','rain_last_tip_ms','rain_since_last_tip_ms','rain_mm'], data);
      renderSimpleTable('bmeBody', ['temp_c','rh_pct','pressure_hpa','light_lux','temp_ground_c','rh_ground_pct'], data);
      renderSimpleTable('airBody', ['pm10_ugm3','pm2_5_ugm3'], data);
      renderSimpleTable('gpsBody', ['gps_lat','gps_lon','gps_alt'], data);
      renderGPS(data);
      loadRain24h();
    }}

    function renderWind(data) {{
      const body = document.getElementById('windBody');
      body.innerHTML = '';
      const rows = Object.values(data).filter(v => v.device_id === 'wind_esp8266' && isWindMetric(v.metric_id));
      rows.sort((a,b) => a.metric_id.localeCompare(b.metric_id));
      const dirDegRow = rows.find(r => r.metric_id === 'wind_direction_deg');
      const dirDeg = dirDegRow ? Number(dirDegRow.value) : undefined;

      rows.forEach(v => {{
        const tr = document.createElement('tr');
        const label = metricLabel(v.metric_id);
        const unit = metricUnit(v.metric_id, v.unit);
        let value = v.value;
        if (v.metric_id === 'wind_direction_cardinal') {{
          value = windCardinal(dirDeg);
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

    async function loadRain24h() {{
      const res = await fetch('/api/history?metric=rain_mm_total');
      const data = await res.json();
      drawRainChart(data);
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
      const video = document.getElementById('video');
      const src = '{hls}';
      if (video.canPlayType('application/vnd.apple.mpegurl')) {{
        video.src = src;
      }} else if (window.Hls && Hls.isSupported()) {{
        const hls = new Hls();
        hls.loadSource(src);
        hls.attachMedia(video);
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
      if (!gallery) return;
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
    loadLatest();
    loadSoundState();
    startVideo();
    loadTimelapseStatus();
    setupTimelapseScroll();
    loadMoreTimelapse();
  </script>
</body>
</html>
""")
