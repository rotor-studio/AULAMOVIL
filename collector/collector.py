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
            topic = f"{self.topic_prefix}/#"
            client.subscribe(topic)
            print(f"[collector] subscribed to {topic}")
        else:
            print(f"[collector] mqtt connect failed rc={rc}")

    def on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            parts = topic.split("/")
            if len(parts) < 4:
                return
            if "/".join(parts[:2]) != "rotor/meteo":
                return
            device_id = parts[2]
            metric_id = parts[3]

            payload = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                return
            if "value" not in payload:
                return

            value = float(payload["value"])
            unit = payload.get("unit")
            ts = parse_timestamp(payload.get("ts"))

            raw_json = json.dumps(payload, separators=(",", ":"))

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
        except Exception as exc:
            print(f"[collector] error: {exc}")

    def run(self):
        host = self.config["mqtt"]["host"]
        port = int(self.config["mqtt"]["port"])
        self.client.connect(host, port, keepalive=60)
        self.client.loop_forever()


if __name__ == "__main__":
    Collector("/opt/rotor-meteo/config/app.yaml").run()
