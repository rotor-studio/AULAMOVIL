import json
import os
import sqlite3
import random
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

import yaml
import paho.mqtt.client as mqtt
try:
    import serial
    import pynmea2
except Exception:  # optional deps
    serial = None
    pynmea2 = None


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_timestamp(value):
    if value is None:
        return time.time()
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
            return time.time()
    return time.time()


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            device_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT,
            raw_json TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS latest (
            device_id TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            ts REAL NOT NULL,
            value REAL NOT NULL,
            unit TEXT,
            raw_json TEXT,
            PRIMARY KEY (device_id, metric_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_metric_ts ON readings(metric_id, ts);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_device_metric_ts ON readings(device_id, metric_id, ts);"
    )
    conn.commit()


class Collector:
    def __init__(self, config_path):
        self.config = load_config(config_path)
        self.topic_prefix = self.config["mqtt"]["topic_prefix"].rstrip("/")
        self.db_path = self.config["storage"]["sqlite_path"]
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        self.latest = {}
        self.latest_lock = threading.Lock()
        self.db_lock = threading.Lock()

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        init_db(self.conn)

        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self.gps_cfg = self.config.get("gps", {})
        self.gps_thread = None

        self.sc_cfg = self.config.get("sensor_community", {})
        self.sc_thread = None

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(f"{self.topic_prefix}/#")
            client.subscribe("meteo/wind/#")
            client.subscribe("meteo/rain/#")
            client.subscribe("meteo/env/#")
            client.subscribe("meteo/light/#")
            client.subscribe("meteo/light_uv/#")
            print(f"[collector] subscribed to {self.topic_prefix}/# and meteo/wind/#")
        else:
            print(f"[collector] mqtt connect failed rc={rc}")

    def store(self, ts, device_id, metric_id, value, unit, raw_json):
        for attempt in range(6):
            try:
                with self.db_lock:
                    with self.conn:
                        self.conn.execute(
                            "INSERT INTO readings (ts, device_id, metric_id, value, unit, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                            (ts, device_id, metric_id, value, unit, raw_json),
                        )
                        self.conn.execute(
                            """
                            INSERT INTO latest (device_id, metric_id, ts, value, unit, raw_json)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(device_id, metric_id)
                            DO UPDATE SET ts=excluded.ts, value=excluded.value, unit=excluded.unit, raw_json=excluded.raw_json
                            """,
                            (device_id, metric_id, ts, value, unit, raw_json),
                        )
                break
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message or attempt == 5:
                    raise
                # Small jitter reduces repeated lock collisions across threads.
                time.sleep(0.15 + random.random() * 0.1)

        with self.latest_lock:
            self.latest[(device_id, metric_id)] = {
                "ts": ts,
                "device_id": device_id,
                "metric_id": metric_id,
                "value": value,
                "unit": unit,
            }

    def handle_rotor_payload(self, topic, payload):
        parts = topic.split("/")
        if len(parts) < 4:
            return
        device_id = parts[2]
        metric_id = parts[3]

        if isinstance(payload, dict):
            if "value" not in payload:
                return
            value = float(payload["value"])
            unit = payload.get("unit")
            ts = parse_timestamp(payload.get("ts"))
            raw_json = json.dumps(payload, separators=(",", ":"))
            self.store(ts, device_id, metric_id, value, unit, raw_json)
            return

        # If payload is a raw number
        try:
            value = float(payload)
        except Exception:
            return
        ts = time.time()
        self.store(ts, device_id, metric_id, value, None, json.dumps({"value": value}))

    def handle_wind_payload(self, topic, payload):
        # Accepts:
        # - meteo/wind/json with JSON payload
        # - meteo/wind/<metric> with raw numeric payload
        parts = topic.split("/")
        if len(parts) < 3:
            return
        device_id = "wind_esp8266"

        if parts[2] == "json" and isinstance(payload, dict):
            ts = parse_timestamp(payload.get("ts"))
            mapping = {
                "raw": ("wind_raw", None),
                "voltage_v": ("wind_voltage_v", "V"),
                "current_ma": ("wind_current_ma", "mA"),
                "speed_ms": ("wind_speed_ms", "m/s"),
                "speed_raw": ("wind_speed_raw", None),
                "speed_ma": ("wind_speed_ma", "mA"),
                "direction_deg": ("wind_direction_deg", "deg"),
                "dir_deg": ("wind_direction_deg", "deg"),
                "direction_raw": ("wind_direction_raw", None),
                "dir_raw": ("wind_direction_raw", None),
                "direction_ma": ("wind_direction_ma", "mA"),
            }
            raw_json = json.dumps(payload, separators=(",", ":"))
            for k, (metric_id, unit) in mapping.items():
                if k in payload:
                    try:
                        value = float(payload[k])
                    except Exception:
                        continue
                    self.store(ts, device_id, metric_id, value, unit, raw_json)
            dir_card = payload.get("direction_cardinal", payload.get("dir_card"))
            if dir_card is not None:
                self.store(ts, device_id, "wind_direction_cardinal", 0.0, None, json.dumps({"direction_cardinal": str(dir_card)}, separators=(",", ":")))
            return

        metric_key = parts[2]
        # Non-json or other metric topic
        metric_map = {
            "raw": "wind_raw",
            "voltage_v": "wind_voltage_v",
            "current_ma": "wind_current_ma",
            "speed_ms": "wind_speed_ms",
            "speed_raw": "wind_speed_raw",
            "speed_ma": "wind_speed_ma",
            "direction_deg": "wind_direction_deg",
            "dir_deg": "wind_direction_deg",
            "direction_raw": "wind_direction_raw",
            "dir_raw": "wind_direction_raw",
            "direction_ma": "wind_direction_ma",
        }
        metric_id = metric_map.get(metric_key)
        if metric_id:
            try:
                value = float(payload)
            except Exception:
                return
            ts = time.time()
            self.store(ts, device_id, metric_id, value, None, json.dumps({"value": value}))
            return

        if metric_key == "direction_cardinal":
            # store as text? keep last in latest via raw_json, value=0
            ts = time.time()
            raw_json = json.dumps({"direction_cardinal": str(payload)}, separators=(",", ":"))
            self.store(ts, device_id, "wind_direction_cardinal", 0.0, None, raw_json)

    def handle_env_payload(self, topic, payload):
        parts = topic.split("/")
        if len(parts) < 3:
            return
        device_id = "bme280_ground"

        metric_map = {
            "temperature_c": ("temp_ground_c", "C"),
            "humidity": ("rh_ground_pct", "pct"),
        }
        metric = parts[2]
        mapped = metric_map.get(metric)
        if not mapped:
            return
        metric_id, unit = mapped
        try:
            value = float(payload)
        except Exception:
            return
        ts = time.time()
        self.store(ts, device_id, metric_id, value, unit, json.dumps({"value": value}, separators=(",", ":")))

    def handle_light_payload(self, topic, payload):
        parts = topic.split("/")
        if len(parts) < 3:
            return
        device_id = "light_mcu"

        if parts[2] == "json" and isinstance(payload, dict):
            ts = parse_timestamp(payload.get("ts"))
            mapping = {
                "lux": ("light_lux", "lux"),
                "uv_raw": ("uv_raw", None),
                "UV_RAW": ("uv_raw", None),
                "uv_v": ("uv_voltage_v", "V"),
                "UV_V": ("uv_voltage_v", "V"),
                "uv_voltage": ("uv_voltage_v", "V"),
                "UV_VOLTAGE": ("uv_voltage_v", "V"),
            }
            raw_json = json.dumps(payload, separators=(",", ":"))
            for key, (metric_id, unit) in mapping.items():
                if key not in payload:
                    continue
                try:
                    value = float(payload[key])
                except Exception:
                    continue
                self.store(ts, device_id, metric_id, value, unit, raw_json)
            return

        metric_map = {
            "lux": ("light_lux", "lux"),
            "uv_raw": ("uv_raw", None),
            "UV_RAW": ("uv_raw", None),
            "uv_v": ("uv_voltage_v", "V"),
            "UV_V": ("uv_voltage_v", "V"),
            "uv_voltage": ("uv_voltage_v", "V"),
            "UV_VOLTAGE": ("uv_voltage_v", "V"),
        }
        mapped = metric_map.get(parts[2])
        if not mapped:
            return
        metric_id, unit = mapped
        try:
            value = float(payload)
        except Exception:
            return
        ts = time.time()
        self.store(ts, device_id, metric_id, value, unit, json.dumps({"value": value}, separators=(",", ":")))

    def handle_rain_payload(self, topic, payload):
        parts = topic.split("/")
        if len(parts) < 3:
            return
        device_id = "rain_node_mcu"

        if parts[2] == "json" and isinstance(payload, dict):
            ts = parse_timestamp(payload.get("ts"))
            mapping = {
                "tips_total": ("rain_tips_total", "count"),
                "mm_total": ("rain_mm_total", "mm"),
                "mm_interval": ("rain_mm_interval", "mm"),
                "rate_mmh": ("rain_rate_mmh", "mm/h"),
                "last_tip_ms": ("rain_last_tip_ms", "ms"),
                "since_last_tip_ms": ("rain_since_last_tip_ms", "ms"),
            }
            for k, (metric_id, unit) in mapping.items():
                if k in payload:
                    try:
                        value = float(payload[k])
                    except Exception:
                        continue
                    raw_json = json.dumps(payload, separators=(",", ":"))
                    self.store(ts, device_id, metric_id, value, unit, raw_json)
            return

        metric_key = parts[2]
        metric_map = {
            "tips_total": "rain_tips_total",
            "mm_total": "rain_mm_total",
            "mm_interval": "rain_mm_interval",
            "rate_mmh": "rain_rate_mmh",
            "last_tip_ms": "rain_last_tip_ms",
            "since_last_tip_ms": "rain_since_last_tip_ms",
        }
        metric_id = metric_map.get(metric_key)
        if metric_id:
            try:
                value = float(payload)
            except Exception:
                return
            ts = time.time()
            self.store(ts, device_id, metric_id, value, None, json.dumps({"value": value}))
            return

    def on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload_raw = msg.payload.decode("utf-8")
            payload = None
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = payload_raw

            if topic.startswith("rotor/meteo/"):
                self.handle_rotor_payload(topic, payload)
            elif topic.startswith("meteo/wind/"):
                self.handle_wind_payload(topic, payload)
            elif topic.startswith("meteo/rain/"):
                self.handle_rain_payload(topic, payload)
            elif topic.startswith("meteo/env/"):
                self.handle_env_payload(topic, payload)
            elif topic.startswith("meteo/light/") or topic.startswith("meteo/light_uv/"):
                self.handle_light_payload(topic, payload)
        except Exception as exc:
            print(f"[collector] error: {exc}")

    def gps_enabled(self):
        return bool(self.gps_cfg.get("enabled", False))

    def gps_read_loop(self):
        if serial is None or pynmea2 is None:
            print("[collector] GPS deps not installed (pyserial, pynmea2).", flush=True)
            return
        device = self.gps_cfg.get("device", "/dev/ttyACM0")
        baud = int(self.gps_cfg.get("baud", 9600))
        device_id = self.gps_cfg.get("device_id", "gps_usb_1")
        min_interval = float(self.gps_cfg.get("min_interval_sec", 1.0))
        last_emit = 0.0
        print(f"[collector] GPS reading from {device} @ {baud}", flush=True)
        while True:
            try:
                with serial.Serial(device, baud, timeout=1) as ser:
                    while True:
                        line = ser.readline().decode("ascii", errors="ignore").strip()
                        if not line.startswith("$"):
                            continue
                        try:
                            msg = pynmea2.parse(line)
                        except Exception:
                            continue
                        now = time.time()
                        if now - last_emit < min_interval:
                            continue

                        lat = None
                        lon = None
                        alt = None
                        valid = False

                        if msg.sentence_type in ("RMC", "GLL"):
                            # RMC has status, GLL has status too
                            status = getattr(msg, "status", None)
                            if status in (None, "A"):
                                lat = msg.latitude if hasattr(msg, "latitude") else None
                                lon = msg.longitude if hasattr(msg, "longitude") else None
                                valid = lat is not None and lon is not None
                        elif msg.sentence_type == "GGA":
                            try:
                                fix = int(getattr(msg, "gps_qual", 0))
                            except Exception:
                                fix = 0
                            if fix > 0:
                                lat = msg.latitude if hasattr(msg, "latitude") else None
                                lon = msg.longitude if hasattr(msg, "longitude") else None
                                try:
                                    alt = float(getattr(msg, "altitude", None))
                                except Exception:
                                    alt = None
                                valid = lat is not None and lon is not None

                        if not valid:
                            continue

                        raw_json = json.dumps(
                            {"lat": lat, "lon": lon, "alt": alt}, separators=(",", ":")
                        )
                        ts = now
                        self.store(ts, device_id, "gps_lat", float(lat), "deg", raw_json)
                        self.store(ts, device_id, "gps_lon", float(lon), "deg", raw_json)
                        if alt is not None:
                            self.store(ts, device_id, "gps_alt", float(alt), "m", raw_json)
                        last_emit = now
            except Exception as exc:
                print(f"[collector] GPS error: {exc}", flush=True)
                time.sleep(2)

    def run(self):
        host = self.config["mqtt"]["host"]
        port = int(self.config["mqtt"]["port"])
        if self.gps_enabled():
            self.gps_thread = threading.Thread(target=self.gps_read_loop, daemon=True)
            self.gps_thread.start()
        if self.sc_cfg.get("enabled", False):
            self.sc_thread = threading.Thread(target=self.sensor_community_loop, daemon=True)
            self.sc_thread.start()
        self.client.connect(host, port, keepalive=60)
        self.client.loop_forever()

    def sensor_community_loop(self):
        url = self.sc_cfg.get("url", "").strip()
        if not url:
            print("[collector] Sensor.Community url missing", flush=True)
            return
        interval = float(self.sc_cfg.get("interval_sec", 60))
        device_id = self.sc_cfg.get("device_id", "sensor_community_1")
        print(f"[collector] Sensor.Community polling {url}", flush=True)
        while True:
            try:
                with urlopen(url, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                values = payload.get("sensordatavalues", [])
                age = payload.get("age")
                try:
                    age_sec = float(age)
                except Exception:
                    age_sec = 0.0
                ts = time.time() - age_sec

                def emit(metric_id, value, unit):
                    numeric = float(value)
                    # Sensor.Community pressure is commonly reported in Pa; normalize to hPa.
                    if metric_id == "pressure_hpa" and numeric > 2000:
                        numeric = numeric / 100.0
                    self.store(ts, device_id, metric_id, numeric, unit, json.dumps(payload, separators=(",", ":")))

                for v in values:
                    key = str(v.get("value_type", "")).strip()
                    val = v.get("value")
                    if val is None:
                        continue
                    key_upper = key.upper()
                    if key_upper == "SDS_P1":
                        emit("pm10_ugm3", val, "ug/m3")
                    elif key_upper == "SDS_P2":
                        emit("pm2_5_ugm3", val, "ug/m3")
                    elif key_upper in ("BME280_TEMPERATURE", "BMP280_TEMPERATURE", "TEMPERATURE"):
                        emit("temp_c", val, "C")
                    elif key_upper in ("BME280_HUMIDITY", "HUMIDITY"):
                        emit("rh_pct", val, "pct")
                    elif key_upper in ("BME280_PRESSURE", "BMP280_PRESSURE", "PRESSURE"):
                        emit("pressure_hpa", val, "hPa")
            except sqlite3.OperationalError as exc:
                print(f"[collector] Sensor.Community sqlite error: {exc}", flush=True)
            except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"[collector] Sensor.Community error: {exc}", flush=True)
            time.sleep(max(5, interval))


if __name__ == "__main__":
    Collector("/opt/rotor-meteo/config/app.yaml").run()
