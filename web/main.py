import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import yaml
from fastapi import FastAPI, Query
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
        radial-gradient(1200px 400px at 20% -10%, rgba(255,255,255,0.8), transparent 60%),
        linear-gradient(180deg, var(--sky-1), var(--sky-2) 35%, var(--sky-3) 70%, var(--sky-4));
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
    @media (max-width: 720px) {{
      .gps-box {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <h1>AULA MOVIL - ROTOR STUDIO</h1>
  <div class=\"tabs\">
    <div class=\"tab active\" data-tab=\"dashboard\">Dashboard</div>
    <div class=\"tab\" data-tab=\"wind\">Viento</div>
    <div class=\"tab\" data-tab=\"rain\">Pluviómetro</div>
    <div class=\"tab\" data-tab=\"bme\">Temp/Humedad/Presión</div>
    <div class=\"tab\" data-tab=\"air\">PM y Aire</div>
    <div class=\"tab\" data-tab=\"gps\">Posición</div>
    <div class=\"tab\" data-tab=\"camera\">Cámara</div>
  </div>

  <div id=\"dashboard\" class=\"panel active\">
    <div id=\"latest\" class=\"grid\">
      <div id=\"dashWind\" class=\"card\"></div>
      <div id=\"dashRain\" class=\"card\"></div>
      <div id=\"dashBme\" class=\"card\"></div>
      <div id=\"dashAir\" class=\"card\"></div>
      <div id=\"dashGps\" class=\"card\"></div>
      <div id=\"dashCam\" class=\"card\"></div>
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
    <h2>Temperatura / Humedad / Presión</h2>
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

    function isOnlineTs(ts) {{
      if (!ts) return false;
      return (Date.now()/1000 - ts) <= ONLINE_MAX_AGE_SEC;
    }}

    function dotHtml(ts) {{
      return `<span class="dot ${{isOnlineTs(ts) ? 'online' : 'offline'}}"></span>`;
    }}

    function metricUnit(id, fallback) {{
      return METRIC_UNITS[id] || fallback || '';
    }}

    function getMetric(data, id) {{
      const key = Object.keys(data).find(k => data[k].metric_id === id);
      return key ? data[key] : null;
    }}

    function max_ts(...items) {{
      return Math.max(...items.map(i => i ? i.ts : 0));
    }}

    function latestTsByDevice(data, deviceId) {{
      const items = Object.values(data).filter(v => v.device_id === deviceId);
      if (!items.length) return 0;
      return Math.max(...items.map(i => i.ts || 0));
    }}

    function setStatus(id, ts) {{
      const dot = document.getElementById(id);
      const text = document.getElementById(id + 'Text');
      const online = isOnlineTs(ts);
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
      const t2 = getMetric(data, 'temp_ground_c');
      const rh2 = getMetric(data, 'rh_ground_pct');
      setCard(document.getElementById('dashBme'), 'Temp / Humedad / Presión', [
        ['Temperatura', t ? `${{t.value}} ${{metricUnit('temp_c', t.unit)}}` : '--'],
        ['Humedad', rh ? `${{rh.value}} ${{metricUnit('rh_pct', rh.unit)}}` : '--'],
        ['Presión', p ? `${{p.value}} ${{metricUnit('pressure_hpa', p.unit)}}` : '--'],
        ['Temp suelo (sombra)', t2 ? `${{t2.value}} ${{metricUnit('temp_ground_c', t2.unit)}}` : '--'],
        ['Humedad suelo (sombra)', rh2 ? `${{rh2.value}} ${{metricUnit('rh_ground_pct', rh2.unit)}}` : '--']
      ], max_ts(t, rh, p, t2, rh2));

      const pm10 = getMetric(data, 'pm10_ugm3');
      const pm25 = getMetric(data, 'pm2_5_ugm3');
      setCard(document.getElementById('dashAir'), 'PM / Calidad del aire', [
        ['PM10', pm10 ? `${{pm10.value}} ${{metricUnit('pm10_ugm3', pm10.unit)}}` : '--'],
        ['PM2.5', pm25 ? `${{pm25.value}} ${{metricUnit('pm2_5_ugm3', pm25.unit)}}` : '--']
      ], max_ts(pm10, pm25));

      renderDashGps(data);
      renderDashCam();

      const bmeTs = Math.max(latestTsByDevice(data, 'bme280_local'), latestTsByDevice(data, 'bme280_ground'));
      setStatus('windStatus', latestTsByDevice(data, 'wind_esp8266'));
      setStatus('rainStatus', latestTsByDevice(data, 'rain_node_mcu'));
      setStatus('bmeStatus', bmeTs);
      setStatus('airStatus', latestTsByDevice(data, 'pm_sensor_1'));
      setStatus('gpsStatus', latestTsByDevice(data, 'gps_usb_1'));

      renderWind(data);
      renderSimpleTable('rainBody', ['rain_tips_total','rain_mm_total','rain_mm_interval','rain_rate_mmh','rain_last_tip_ms','rain_since_last_tip_ms','rain_mm'], data);
      renderSimpleTable('bmeBody', ['temp_c','rh_pct','pressure_hpa','temp_ground_c','rh_ground_pct'], data);
      renderSimpleTable('airBody', ['pm10_ugm3','pm2_5_ugm3'], data);
      renderSimpleTable('gpsBody', ['gps_lat','gps_lon','gps_alt'], data);
      renderGPS(data);
      loadRain24h();
    }}

    function renderWind(data) {{
      const body = document.getElementById('windBody');
      body.innerHTML = '';
      const rows = Object.values(data).filter(v => v.device_id === 'wind_esp8266');
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
    startVideo();
    loadTimelapseStatus();
    setupTimelapseScroll();
    loadMoreTimelapse();
  </script>
</body>
</html>
""")
