import os
import subprocess
import time

import yaml


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


def build_rtsp_url(cfg):
    cam = cfg.get("camera", {})
    host = cam.get("host")
    if not host:
        raise RuntimeError("camera.host not set")
    user = cam.get("username") or ""
    pwd = cam.get("password") or ""
    path = cam.get("rtsp_path") or "/Streaming/Channels/101"
    auth = ""
    if user:
        auth = f"{user}:{pwd}@" if pwd else f"{user}@"
    return f"rtsp://{auth}{host}:554{path}"


def main():
    cfg = load_config()
    hls_dir = cfg.get("camera", {}).get("hls_dir") or "/opt/rotor-meteo/data/hls"
    os.makedirs(hls_dir, exist_ok=True)

    rtsp_url = build_rtsp_url(cfg)
    playlist = os.path.join(hls_dir, "stream.m3u8")

    cmd = [
        "ffmpeg",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-c:v",
        "copy",
        "-an",
        "-f",
        "hls",
        "-hls_time",
        "2",
        "-hls_list_size",
        "5",
        "-hls_flags",
        "delete_segments+append_list",
        "-hls_allow_cache",
        "0",
        playlist,
    ]

    while True:
        try:
            subprocess.run(cmd, check=False)
        except Exception:
            pass
        time.sleep(2)


if __name__ == "__main__":
    main()
