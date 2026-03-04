import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import yaml
import paho.mqtt.client as mqtt


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

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        init_db(self.conn)

        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(f"{self.topic_prefix}/#")
            client.subscribe("meteo/wind/#")
            print(f"[collector] subscribed to {self.topic_prefix}/# and meteo/wind/#")
        else:
            print(f"[collector] mqtt connect failed rc={rc}")

    def store(self, ts, device_id, metric_id, value, unit, raw_json):
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

        # Non-json or other metric topic
        metric_key = parts[2]
        metric_id = {
            "raw": "wind_raw",
            "voltage_v": "wind_voltage_v",
            "current_ma": "wind_current_ma",
            "speed_ms": "wind_speed_ms",
        }.get(metric_key)
        if not metric_id:
            return
        try:
            value = float(payload)
        except Exception:
            return
        ts = time.time()
        self.store(ts, device_id, metric_id, value, None, json.dumps({"value": value}))

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
        except Exception as exc:
            print(f"[collector] error: {exc}")

    def run(self):
        host = self.config["mqtt"]["host"]
        port = int(self.config["mqtt"]["port"])
        self.client.connect(host, port, keepalive=60)
        self.client.loop_forever()


if __name__ == "__main__":
    Collector("/opt/rotor-meteo/config/app.yaml").run()
