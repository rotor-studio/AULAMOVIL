import asyncio
import json
import os
import sqlite3
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

# Optional human-friendly overrides
LABEL_OVERRIDES = {
    "wind_speed_ms": "Velocidad del viento",
    "wind_direction_deg": "Direcci?n del viento",
    "wind_direction_cardinal": "Direcci?n del viento (cardinal)",
}

app = FastAPI(title="Rotor Meteo")
app.mount("/static", StaticFiles(directory="/opt/rotor-meteo/web/static"), name="static")
app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")


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
  <title>Rotor Meteo</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 16px; }}
    .tabs {{ display: flex; gap: 8px; margin-bottom: 12px; }}
    .tab {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; cursor: pointer; }}
    .tab.active {{ background: #eee; }}
    .panel {{ display: none; }}
    .panel.active {{ display: block; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 10px; }}
    canvas {{ width: 100%; height: 200px; border: 1px solid #eee; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, \"Liberation Mono\", monospace; }}
    video {{ width: 100%; max-width: 960px; background: #000; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 720px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
    .wind-card .row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .compass {{ width: 120px; height: 120px; border: 2px solid #333; border-radius: 50%; position: relative; }}
    .compass .needle {{ position: absolute; left: 50%; top: 10%; width: 2px; height: 45%; background: #c0392b; transform-origin: bottom center; }}
    .compass .center {{ position: absolute; left: 50%; top: 50%; width: 8px; height: 8px; background: #333; border-radius: 50%; transform: translate(-50%, -50%); }}
    .cam-thumb {{ width: 100%; max-width: 240px; border: 1px solid #ddd; border-radius: 6px; margin-top: 6px; }}
    .gps-box {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-start; }}
    .gps-map {{ width: 320px; height: 220px; border: 1px solid #ddd; border-radius: 6px; }}
    .gps-actions {{ display: flex; gap: 8px; margin-top: 6px; }}
    .gps-actions button {{ padding: 6px 10px; }}
  </style>
</head>
<body>
  <h1>Rotor Meteo</h1>
  <div class=\"tabs\">
    <div class=\"tab active\" data-tab=\"dashboard\">Dashboard</div>
    <div class=\"tab\" data-tab=\"wind\">Wind</div>
    <div class=\"tab\" data-tab=\"rain\">Pluviometro</div>
    <div class=\"tab\" data-tab=\"bme\">Temp/Humedad/Presion</div>
    <div class=\"tab\" data-tab=\"air\">PM & Aire</div>
    <div class=\"tab\" data-tab=\"gps\">Posicion</div>
    <div class=\"tab\" data-tab=\"camera\">Camera</div>
  </div>

  <div id=\"dashboard\" class=\"panel active\">
    <div id=\"latest\" class=\"grid\"></div>

    <h2>History</h2>
    <label>Metric: <input id=\"metric\" placeholder=\"temp_c\" /></label>
    <button onclick=\"loadHistory()\">Load</button>
    <canvas id=\"chart\" width=\"600\" height=\"200\"></canvas>
  </div>

  <div id=\"wind\" class=\"panel\">
    <h2>Wind (ESP8266)</h2>
    <table>
      <thead><tr><th>Metric</th><th>Value</th><th>Updated</th></tr></thead>
      <tbody id=\"windBody\"></tbody>
    </table>
    <h3>Velocidad del viento (tiempo real)</h3>
    <canvas id=\"windChart\" width=\"700\" height=\"220\"></canvas>
  </div>

  <div id=\"rain\" class=\"panel\">
    <h2>Pluviometro</h2>
    <table>
      <thead><tr><th>Metrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"rainBody\"></tbody>
    </table>
  </div>

  <div id=\"bme\" class=\"panel\">
    <h2>Temperatura / Humedad / Presion</h2>
    <table>
      <thead><tr><th>Metrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"bmeBody\"></tbody>
    </table>
  </div>

  <div id=\"air\" class=\"panel\">
    <h2>PM10 / PM2.5 y Calidad del Aire</h2>
    <table>
      <thead><tr><th>Metrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"airBody\"></tbody>
    </table>
  </div>

  <div id=\"gps\" class=\"panel\">
    <h2>Posicion (GPS)</h2>
    <table>
      <thead><tr><th>Metrica</th><th>Valor</th><th>Actualizado</th></tr></thead>
      <tbody id=\"gpsBody\"></tbody>
    </table>
  </div>

  <div id=\"camera\" class=\"panel\">
    <h2>Camera (Live)</h2>
    <video id=\"video\" controls autoplay muted playsinline></video>
    <p>RTSP URL:</p>
    <div class=\"mono\">{rtsp}</div>
    <p>
      If video does not start, use VLC with the RTSP URL above.
    </p>
  </div>

  <script src=\"/static/hls.min.js\"></script>
  <script>
    const METRIC_NAMES = {json.dumps({k: (LABEL_OVERRIDES.get(k) or v.get('name')) for k, v in REGISTRY.get('metrics', {}).items()})};
    const METRIC_UNITS = {json.dumps({k: v.get('unit') for k, v in REGISTRY.get('metrics', {}).items()})};

    const windSeries = [];
    const WIND_MAX_POINTS = 120;

    const WIND_METRIC_PREFIX = 'wind_';

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
        <strong>Viento</strong>
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

    function buildSummaryCard(title, rows, ts) {{
      const card = document.createElement('div');
      card.className = 'card';
      const timeText = ts ? new Date(ts*1000).toLocaleString() : '';
      const rowsHtml = rows.map(r => `<div>${{r[0]}}: ${{r[1]}}</div>`).join('');
      card.innerHTML = `<strong>${{title}}</strong>${{rowsHtml}}<div><small>${{timeText}}</small></div>`;
      return card;
    }}

    function updateCameraThumb() {{
      const img = document.getElementById(\"camThumb\");
      if (!img) return;
      img.src = `/hls/latest.jpg?ts=${{Date.now()}}`;
    }}

    function buildCameraCard() {{
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `<strong>Camara</strong><div>Estado: <span id="camStatus">Comprobando...</span></div><img id="camThumb" class="cam-thumb" src="/hls/latest.jpg" /><div><small>Ver pestana Camera</small></div>`;
      fetch('/hls/stream.m3u8', {{ method: 'GET', cache: 'no-store' }})
        .then(r => {{ document.getElementById('camStatus').textContent = r.ok ? 'Online' : 'Offline'; }})
        .catch(() => {{ document.getElementById('camStatus').textContent = 'Offline'; }});
      return card;
    }}


    async function loadLatest() {{
      const res = await fetch('/api/latest');
      const data = await res.json();
      const el = document.getElementById('latest');
      el.innerHTML = '';

      const windCard = buildWindCard(data);
      if (windCard) el.appendChild(windCard);

      const rain = getMetric(data, 'rain_mm');
      el.appendChild(buildSummaryCard('Pluviometro', [
        ['Lluvia acumulada', rain ? `${{rain.value}} ${{metricUnit('rain_mm', rain.unit)}}` : '--']
      ], rain ? rain.ts : 0));

      el.appendChild(buildBmeSummary(data));

      const pm10 = getMetric(data, 'pm10_ugm3');
      const pm25 = getMetric(data, 'pm2_5_ugm3');
      el.appendChild(buildSummaryCard('PM / Calidad del aire', [
        ['PM10', pm10 ? `${{pm10.value}} ${{metricUnit('pm10_ugm3', pm10.unit)}}` : '--'],
        ['PM2.5', pm25 ? `${{pm25.value}} ${{metricUnit('pm2_5_ugm3', pm25.unit)}}` : '--']
      ], max_ts(pm10, pm25)));

      const camCard = buildCameraCard();
      if (camCard) el.appendChild(camCard);

      renderWind(data);
      renderSimpleTable('rainBody', ['rain_mm'], data);
      renderSimpleTable('bmeBody', ['temp_c','rh_pct','pressure_hpa'], data);
      renderSimpleTable('airBody', ['pm10_ugm3','pm2_5_ugm3'], data);
      renderSimpleTable('gpsBody', ['gps_lat','gps_lon','gps_alt'], data);
      renderGPS(data);
    }}



    function renderGPS(data) {{
      const lat = getMetric(data, 'gps_lat');
      const lon = getMetric(data, 'gps_lon');
      const alt = getMetric(data, 'gps_alt');

      const gpsText = document.getElementById('gpsText');
      const gpsMap = document.getElementById('gpsMap');
      const gpsLink = document.getElementById('gpsOsmLink');
      if (!gpsText || !gpsMap || !gpsLink) return;

      if (!lat || !lon) {{
        gpsText.textContent = 'Sin datos';
        gpsMap.src = '';
        gpsLink.removeAttribute('href');
        return;
      }}

      const latv = Number(lat.value);
      const lonv = Number(lon.value);
      const altv = alt ? Number(alt.value) : null;
      const dms = toDMS(latv, lonv);
      gpsText.textContent = `Lat: ${{latv.toFixed(6)}}  Lon: ${{lonv.toFixed(6)}}${{altv !== null ? `  Alt: ${{altv.toFixed(1)}} m` : ''}}  (${{dms}})`;

      const delta = 0.01;
      const bbox = `${{lonv-delta}},${{latv-delta}},${{lonv+delta}},${{latv+delta}}`;
      gpsMap.src = `https://www.openstreetmap.org/export/embed.html?bbox=${{bbox}}&layer=mapnik&marker=${{latv}},${{lonv}}`;
      gpsLink.href = `https://www.openstreetmap.org/?mlat=${{latv}}&mlon=${{lonv}}#map=15/${{latv}}/${{lonv}}`;
    }}

    function toDMS(lat, lon) {{
      function conv(v, pos, neg) {{
        const dir = v >= 0 ? pos : neg;
        const av = Math.abs(v);
        const deg = Math.floor(av);
        const minFloat = (av - deg) * 60;
        const min = Math.floor(minFloat);
        const sec = ((minFloat - min) * 60).toFixed(1);
        return `${{deg}}d${{min}}'${{sec}}" ${{dir}}`;
      }}
      return `${{conv(lat, 'N', 'S')}}, ${{conv(lon, 'E', 'O')}}`;
    }}

    function copyGps() {{
      const gpsText = document.getElementById('gpsText');
      if (!gpsText) return;
      const text = gpsText.textContent || '';
      if (navigator.clipboard && text) {{
        navigator.clipboard.writeText(text);
      }}
    }}

    function buildBmeSummary(data) {{
      const entries = Object.values(data).filter(v => ['temp_c','rh_pct','pressure_hpa'].includes(v.metric_id));
      const byDevice = {{}};
      entries.forEach(v => {{
        if (!byDevice[v.device_id]) byDevice[v.device_id] = {{}};
        byDevice[v.device_id][v.metric_id] = v;
      }});

      const deviceIds = Object.keys(byDevice);
      if (deviceIds.length === 0) {{
        return buildSummaryCard('Temp / Humedad / Presion', [
          ['Temperatura', '--'],
          ['Humedad', '--'],
          ['Presion', '--']
        ], 0);
      }}

      deviceIds.sort((a,b) => (a === 'bme280_local') ? -1 : (b === 'bme280_local') ? 1 : a.localeCompare(b));
      const main = deviceIds[0];
      const secondary = deviceIds[1];

      const mainT = byDevice[main].temp_c ? `${{byDevice[main].temp_c.value}} ${{metricUnit('temp_c', byDevice[main].temp_c.unit)}}` : '--';
      const mainRH = byDevice[main].rh_pct ? `${{byDevice[main].rh_pct.value}} ${{metricUnit('rh_pct', byDevice[main].rh_pct.unit)}}` : '--';
      const mainP = byDevice[main].pressure_hpa ? `${{byDevice[main].pressure_hpa.value}} ${{metricUnit('pressure_hpa', byDevice[main].pressure_hpa.unit)}}` : '--';

      const rows = [
        ['Temperatura', mainT],
        ['Humedad', mainRH],
        ['Presion', mainP],
      ];

      if (secondary) {{
        const sec = byDevice[secondary];
        let label = 'Sensor 2';
        const lid = secondary.toLowerCase();
        if (lid.includes('ground') || lid.includes('suelo')) label = 'Suelo';
        if (lid.includes('shade') || lid.includes('sombra')) label = 'Sombra';

        const secT = sec.temp_c ? `${{sec.temp_c.value}} ${{metricUnit('temp_c', sec.temp_c.unit)}}` : '--';
        const secRH = sec.rh_pct ? `${{sec.rh_pct.value}} ${{metricUnit('rh_pct', sec.rh_pct.unit)}}` : '--';
        rows.push([`Temp (${{label}})`, secT]);
        rows.push([`Hum (${{label}})`, secRH]);
      }}

      const ts = Math.max(...deviceIds.map(d => Math.max(
        byDevice[d].temp_c ? byDevice[d].temp_c.ts : 0,
        byDevice[d].rh_pct ? byDevice[d].rh_pct.ts : 0,
        byDevice[d].pressure_hpa ? byDevice[d].pressure_hpa.ts : 0,
      )));

      return buildSummaryCard('Temp / Humedad / Presion', rows, ts);
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

    const evt = new EventSource('/api/stream');
    evt.onmessage = () => loadLatest();
    loadLatest();
    startVideo();
  </script>
</body>
</html>
""")




