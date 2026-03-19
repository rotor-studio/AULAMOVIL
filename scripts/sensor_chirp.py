#!/opt/rotor-meteo/.venv/bin/python
import argparse
import json
import math
import os
import struct
import subprocess
import tempfile
import time
import urllib.request
import wave


API_URL = "http://127.0.0.1:8000/api/latest"
AUDIO_DEVICE = "plughw:CARD=CD002,DEV=0"
SAMPLE_RATE = 22050


def fetch_latest():
    with urllib.request.urlopen(API_URL, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_metric(data, metric_id, device_id=None):
    rows = [v for v in data.values() if v.get("metric_id") == metric_id]
    if device_id:
        rows = [v for v in rows if v.get("device_id") == device_id]
    if not rows:
        return None
    rows.sort(key=lambda item: item.get("ts") or 0, reverse=True)
    return rows[0]


def build_chirp_wav(path, base_hz=1200.0, span_hz=900.0, duration=0.9):
    total = int(SAMPLE_RATE * duration)
    frames = bytearray()
    for i in range(total):
        t = i / SAMPLE_RATE
        env = math.sin(math.pi * min(1.0, t / duration)) ** 2
        chirp = math.sin(2 * math.pi * (base_hz + span_hz * math.sin(2 * math.pi * 3.5 * t)) * t)
        overtone = math.sin(2 * math.pi * (base_hz * 1.8 + span_hz * 0.15 * math.cos(2 * math.pi * 5.2 * t)) * t)
        sample = 0.42 * chirp + 0.18 * overtone
        sample *= env
        value = max(-1.0, min(1.0, sample))
        pcm = int(value * 32767)
        frames.extend(struct.pack("<h", pcm))

    with wave.open(path, "wb") as wavf:
        wavf.setnchannels(1)
        wavf.setsampwidth(2)
        wavf.setframerate(SAMPLE_RATE)
        wavf.writeframes(bytes(frames))


def play_chirp(device=AUDIO_DEVICE):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        build_chirp_wav(wav_path)
        subprocess.run(
            ["aplay", "-D", device, wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Bird-like chirp on sensor changes.")
    parser.add_argument("--metric", default="light_lux")
    parser.add_argument("--device", default="wind_esp8266")
    parser.add_argument("--threshold", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--cooldown", type=float, default=8.0)
    parser.add_argument("--play-now", action="store_true")
    args = parser.parse_args()

    if args.play_now:
        print("[chirp] manual test")
        play_chirp()
        return

    print(
        f"[chirp] watching {args.device}/{args.metric} "
        f"threshold={args.threshold} interval={args.interval}s cooldown={args.cooldown}s"
    )

    last_value = None
    last_play = 0.0

    while True:
        try:
            data = fetch_latest()
            row = latest_metric(data, args.metric, args.device)
            if row is None:
                print("[chirp] metric not found")
                time.sleep(args.interval)
                continue

            value = float(row["value"])
            if last_value is None:
                last_value = value
                print(f"[chirp] baseline={value:.2f}")
            else:
                delta = abs(value - last_value)
                now = time.time()
                if delta >= args.threshold and (now - last_play) >= args.cooldown:
                    print(f"[chirp] change detected value={value:.2f} delta={delta:.2f}")
                    play_chirp()
                    last_play = now
                last_value = value
        except KeyboardInterrupt:
            print("[chirp] stopped")
            return
        except Exception as exc:
            print(f"[chirp] error: {exc}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
