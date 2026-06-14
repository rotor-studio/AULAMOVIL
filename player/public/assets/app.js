const state = {
  routes: [],
  route: null,
  points: [],
  metrics: [],
  metric: null,
  index: 0,
  playing: false,
  timer: null,
  csrf: null,
  private: false,
  markers: [],
  recording: false,
  recordFrames: [],
};

const metricLabels = {
  gps_speed_kmh: ["Velocidad", "km/h", 1],
  bme280_local__temp_c: ["Temp. local", "degC", 1],
  bme280_local__rh_pct: ["HR local", "%", 1],
  bme280_ground__temp_ground_c: ["Temp. suelo", "degC", 1],
  bme280_ground__rh_ground_pct: ["HR suelo", "%", 1],
  bme280_ground__pressure_ground_hpa: ["Presion suelo", "hPa", 1],
  pm_sensor_1__pm10_ugm3: ["PM10", "ug/m3", 2],
  pm_sensor_1__pm2_5_ugm3: ["PM2.5", "ug/m3", 2],
  sensor_community_1__pm10_ugm3: ["PM10", "ug/m3", 2],
  sensor_community_1__pm2_5_ugm3: ["PM2.5", "ug/m3", 2],
  sensor_community_1__temp_c: ["Temp. aire", "degC", 1],
  sensor_community_1__rh_pct: ["HR aire", "%", 1],
  sensor_community_1__pressure_hpa: ["Presion aire", "hPa", 1],
  wind_1__wind_speed_ms: ["Viento", "m/s", 2],
  wind_1__wind_dir_deg: ["Dir. viento", "deg", 0],
  wind_esp8266__wind_speed_ms: ["Viento", "m/s", 2],
  wind_esp8266__wind_direction_deg: ["Dir. viento", "deg", 0],
  wind_esp8266__wind_direction_cardinal: ["Dir. cardinal", "", 0],
  light_mcu__light_lux: ["Luz", "lux", 0],
  light_mcu__uv_raw: ["UV raw", "", 0],
  rain_gauge_1__rain_mm: ["Lluvia", "mm", 2],
  rain_node_mcu__rain_mm_total: ["Lluvia total", "mm", 2],
};

const exportMetricOrder = [
  "bme280_ground__temp_ground_c",
  "bme280_ground__rh_ground_pct",
  "bme280_ground__pressure_ground_hpa",
  "pm_sensor_1__pm10_ugm3",
  "pm_sensor_1__pm2_5_ugm3",
  "sensor_community_1__pm10_ugm3",
  "sensor_community_1__pm2_5_ugm3",
  "wind_1__wind_speed_ms",
  "wind_1__wind_dir_deg",
  "wind_esp8266__wind_speed_ms",
  "wind_esp8266__wind_direction_deg",
  "wind_esp8266__wind_direction_cardinal",
  "light_mcu__light_lux",
  "light_mcu__uv_raw",
  "sensor_community_1__temp_c",
  "sensor_community_1__rh_pct",
  "sensor_community_1__pressure_hpa",
  "bme280_local__temp_c",
  "bme280_local__rh_pct",
];

const routeSelect = document.querySelector("#routeSelect");
const metricSelect = document.querySelector("#metricSelect");
const emojiToggle = document.querySelector("#emojiToggle");
const timeline = document.querySelector("#timeline");
const playButton = document.querySelector("#playButton");
const playSpeedSelect = document.querySelector("#playSpeedSelect");
const timeLabel = document.querySelector("#timeLabel");
const dataPanel = document.querySelector("#dataPanel");
const loginForm = document.querySelector("#loginForm");
const passwordInput = document.querySelector("#passwordInput");
const privateTools = document.querySelector("#privateTools");
const privateStatus = document.querySelector("#privateStatus");
const uploadForm = document.querySelector("#uploadForm");
const routeTitleInput = document.querySelector("#routeTitleInput");
const csvInput = document.querySelector("#csvInput");
const pickCsvButton = document.querySelector("#pickCsvButton");
const csvFileName = document.querySelector("#csvFileName");
const publicInput = document.querySelector("#publicInput");
const logoutButton = document.querySelector("#logoutButton");
const exportPngButton = document.querySelector("#exportPngButton");
const recordPlayButton = document.querySelector("#recordPlayButton");
const routeRenameInput = document.querySelector("#routeRenameInput");
const renameRouteButton = document.querySelector("#renameRouteButton");
const deleteRouteButton = document.querySelector("#deleteRouteButton");
const exportFormatSelect = document.querySelector("#exportFormatSelect");
const exportMapSelect = document.querySelector("#exportMapSelect");
const exportZoomSelect = document.querySelector("#exportZoomSelect");
const exportScaleSelect = document.querySelector("#exportScaleSelect");

const map = L.map("map", { preferCanvas: true });
const layers = {
  "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 20,
    attribution: "&copy; OpenStreetMap",
  }),
  "CARTO claro": L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    maxZoom: 20,
    attribution: "&copy; OpenStreetMap &copy; CARTO",
  }),
  "Satelite Esri": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 19,
    attribution: "Tiles &copy; Esri",
  }),
};
layers["CARTO claro"].addTo(map);
L.control.layers(layers, {}, { collapsed: false, position: "topright" }).addTo(map);
map.setView([43.556, -5.924], 14);

let pathLine = null;
let progressLine = null;
let currentMarker = null;
let useTemperatureEmoji = false;
const tileCache = new Map();

async function api(path, options = {}) {
  const url = path.startsWith("/?") ? path.slice(1) : path;
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Error");
  return payload;
}

function formatValue(value, unit = "", digits = 1) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toLocaleString("es-ES", { maximumFractionDigits: digits, minimumFractionDigits: digits })}${unit ? ` ${unit}` : ""}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function formatTime(value) {
  return new Intl.DateTimeFormat("es-ES", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function numberValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function metricLabel(key) {
  return metricLabels[key] || [key, "", 1];
}

function exportDimensions() {
  const formats = {
    landscape: [1920, 1080],
    portrait: [1080, 1920],
    square: [1600, 1600],
  };
  const [baseWidth, baseHeight] = formats[exportFormatSelect.value] || formats.landscape;
  const scale = Number(exportScaleSelect.value) || 1;
  return {
    width: baseWidth * scale,
    height: baseHeight * scale,
    scale,
  };
}

function shouldHideDataRow(key, value) {
  const number = numberValue(value);
  return key === "gps_speed_kmh"
    || key === "wind_esp8266__wind_direction_cardinal"
    || (key.startsWith("rain_node_mcu__") && number === 0);
}

function metricRange(key) {
  const values = state.points.map((point) => numberValue(point.measures[key])).filter((value) => value !== null);
  if (!values.length) return { min: null, max: null };
  return { min: Math.min(...values), max: Math.max(...values) };
}

function interpolateColor(start, end, amount) {
  const a = parseInt(start.slice(1), 16);
  const b = parseInt(end.slice(1), 16);
  const ar = a >> 16;
  const ag = (a >> 8) & 255;
  const ab = a & 255;
  const br = b >> 16;
  const bg = (b >> 8) & 255;
  const bb = b & 255;
  const rr = Math.round(ar + amount * (br - ar));
  const rg = Math.round(ag + amount * (bg - ag));
  const rb = Math.round(ab + amount * (bb - ab));
  return `rgb(${rr}, ${rg}, ${rb})`;
}

function colorFor(value, range) {
  if (value === null || range.max === null || range.max === range.min) return "#7e8790";
  const t = Math.max(0, Math.min(1, (value - range.min) / (range.max - range.min)));
  if (t < 0.33) return interpolateColor("#2468ac", "#47a86b", t / 0.33);
  if (t < 0.66) return interpolateColor("#47a86b", "#f2c14e", (t - 0.33) / 0.33);
  return interpolateColor("#f2c14e", "#c64737", (t - 0.66) / 0.34);
}

function clearRoute() {
  state.markers.forEach((marker) => marker.remove());
  state.markers = [];
  if (pathLine) pathLine.remove();
  if (progressLine) progressLine.remove();
  if (currentMarker) currentMarker.remove();
  pathLine = null;
  progressLine = null;
  currentMarker = null;
}

function isTemperatureMetric(key) {
  return key === "bme280_ground__temp_ground_c" || key === "sensor_community_1__temp_c";
}

function emojiForTemperature(value) {
  const number = numberValue(value);
  if (number === null) return "😐";
  if (number >= 33) return "🥵";
  if (number >= 30) return "😅";
  if (number >= 29) return "😎";
  if (number >= 24) return "🙂";
  if (number >= 18) return "😊";
  if (number >= 10) return "😐";
  return "🥶";
}

function currentIcon(point = null) {
  if (useTemperatureEmoji && isTemperatureMetric(state.metric) && point) {
    return L.divIcon({
      className: "",
      html: `<div class="current-marker emoji-current">${emojiForTemperature(point.measures[state.metric])}</div>`,
      iconSize: [48, 48],
      iconAnchor: [24, 24],
    });
  }

  return L.divIcon({
    className: "",
    html: '<div class="current-marker"></div>',
    iconSize: [38, 38],
    iconAnchor: [19, 19],
  });
}

function updateEmojiToggle() {
  const enabled = isTemperatureMetric(state.metric);
  emojiToggle.disabled = !enabled;
  emojiToggle.classList.toggle("active", useTemperatureEmoji && enabled);
  if (currentMarker && state.points.length) {
    currentMarker.setIcon(currentIcon(state.points[state.index]));
  }
}

function updatePrivateRouteTools() {
  const hasRoute = Boolean(state.private && state.route);
  routeRenameInput.disabled = !hasRoute;
  renameRouteButton.disabled = !hasRoute;
  deleteRouteButton.disabled = !hasRoute;
  exportPngButton.disabled = !state.route || !state.points.length;
  recordPlayButton.disabled = !state.route || !state.points.length;
  routeRenameInput.value = hasRoute ? state.route.title : "";
}

function renderMetric() {
  const range = metricRange(state.metric);
  state.markers.forEach((marker, index) => {
    const value = numberValue(state.points[index].measures[state.metric]);
    const color = colorFor(value, range);
    marker.setStyle({ color, fillColor: color });
  });
}

function renderRoute() {
  clearRoute();
  if (!state.points.length) return;
  const latLngs = state.points.map((point) => [point.gps_lat, point.gps_lon]);
  pathLine = L.polyline(latLngs, { color: "#263238", weight: 4, opacity: 0.45 }).addTo(map);
  progressLine = L.polyline([latLngs[0]], { color: "#58bdf0", weight: 5, opacity: 0.95 }).addTo(map);
  state.markers = state.points.map((point, index) => L.circleMarker([point.gps_lat, point.gps_lon], {
    radius: 7,
    weight: 0,
    opacity: 1,
    fillOpacity: 0.86,
  }).on("click", () => updateCurrent(index, false)).addTo(map));
  currentMarker = L.marker(latLngs[0], { icon: currentIcon(state.points[0]), zIndexOffset: 1000 }).addTo(map);
  timeline.max = String(state.points.length - 1);
  renderMetric();
  updateCurrent(0, false);
  map.fitBounds(pathLine.getBounds(), { padding: [36, 36] });
}

function updateCurrent(index, shouldPan = true) {
  state.index = Math.max(0, Math.min(state.points.length - 1, index));
  const point = state.points[state.index];
  const latLng = [point.gps_lat, point.gps_lon];
  timeline.value = String(state.index);
  timeLabel.textContent = formatTime(point.captured_at_iso);
  if (currentMarker) currentMarker.setLatLng(latLng);
  if (currentMarker) currentMarker.setIcon(currentIcon(point));
  if (progressLine) {
    progressLine.setLatLngs(state.points.slice(0, state.index + 1).map((item) => [item.gps_lat, item.gps_lon]));
  }

  const coreRows = [
    ["Hora", formatTime(point.captured_at_iso)],
    ["Altitud", formatValue(point.gps_alt, "m", 1)],
    ["Velocidad", formatValue(point.measures.gps_speed_kmh, "km/h", 1)],
    ["Lat / Lon", `${point.gps_lat.toFixed(6)}, ${point.gps_lon.toFixed(6)}`],
  ];
  const measureRows = state.metrics
    .filter((key) => !shouldHideDataRow(key, point.measures[key]))
    .map((key) => {
      const [label, unit, digits] = metricLabel(key);
      return [label, formatValue(point.measures[key], unit, digits)];
    });
  dataPanel.innerHTML = [...coreRows, ...measureRows]
    .map(([name, value]) => `<div class="data-row"><span>${escapeHtml(name)}</span><span class="value">${escapeHtml(value)}</span></div>`)
    .join("");
  if (shouldPan) map.panTo(latLng, { animate: true, duration: 0.25 });
}

function setPlaying(nextPlaying) {
  state.playing = nextPlaying;
  playButton.textContent = state.playing ? "||" : ">";
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (!state.playing) return;
  const speed = Math.max(1, Number(playSpeedSelect.value) || 1);
  state.timer = setInterval(() => {
    const next = state.index + 1;
    if (next >= state.points.length) {
      setPlaying(false);
      if (state.recording) stopRecording();
      return;
    }
    updateCurrent(next);
  }, 180 / speed);
}

function distanceMeters(a, b) {
  const radius = 6371000;
  const lat1 = (a.gps_lat * Math.PI) / 180;
  const lat2 = (b.gps_lat * Math.PI) / 180;
  const dLat = ((b.gps_lat - a.gps_lat) * Math.PI) / 180;
  const dLon = ((b.gps_lon - a.gps_lon) * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return radius * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
}

function applyDerivedMeasures(points) {
  points.forEach((point, index) => {
    if (!point.measures) point.measures = {};
    if (index === 0) {
      point.measures.gps_speed_kmh = 0;
      return;
    }
    const previous = points[index - 1];
    const seconds = (new Date(point.captured_at_iso) - new Date(previous.captured_at_iso)) / 1000;
    point.measures.gps_speed_kmh = seconds > 0 ? (distanceMeters(previous, point) / seconds) * 3.6 : 0;
  });
}

function deriveMetrics(points) {
  const keys = new Set();
  points.forEach((point) => {
    Object.entries(point.measures || {}).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined && Number.isFinite(Number(value))) keys.add(key);
    });
  });
  return keys.has("gps_speed_kmh")
    ? ["gps_speed_kmh", ...[...keys].filter((key) => key !== "gps_speed_kmh")]
    : [...keys];
}

async function loadRoutes() {
  const payload = await api("/?api=routes");
  const currentRouteId = state.route ? String(state.route.id) : null;
  state.routes = payload.routes;
  state.csrf = payload.csrf;
  state.private = payload.private;
  privateTools.classList.toggle("hidden", !state.private);
  loginForm.classList.toggle("hidden", state.private);
  setTimeout(() => map.invalidateSize(), 0);
  routeSelect.innerHTML = state.routes.map((route) => `<option value="${route.id}">${escapeHtml(route.title)}</option>`).join("");
  const nextRoute = state.routes.find((route) => String(route.id) === currentRouteId) || state.routes[0];
  if (nextRoute) {
    routeSelect.value = String(nextRoute.id);
    await loadRoute(nextRoute.id);
  } else {
    clearRoute();
    state.route = null;
    state.points = [];
    state.metrics = [];
    metricSelect.innerHTML = "";
    dataPanel.innerHTML = '<div class="data-row"><span>No hay rutas publicas todavia.</span></div>';
    updatePrivateRouteTools();
  }
}

async function loadRoute(id) {
  const payload = await api(`/?api=route&id=${encodeURIComponent(id)}`);
  state.route = payload.route;
  state.points = payload.points;
  applyDerivedMeasures(state.points);
  state.metrics = deriveMetrics(state.points);
  state.metric = state.metrics.includes("bme280_ground__temp_ground_c")
    ? "bme280_ground__temp_ground_c"
    : (state.metrics.includes(state.metric) ? state.metric : state.metrics[0]);
  metricSelect.innerHTML = state.metrics.map((key) => {
    const [label] = metricLabel(key);
    return `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`;
  }).join("");
  metricSelect.value = state.metric;
  renderRoute();
  updateEmojiToggle();
  updatePrivateRouteTools();
}

async function exportPng() {
  if (!state.points.length) return;
  try {
    privateStatus.textContent = "Exportando PNG...";
    const canvas = await htmlCanvas();
    const link = document.createElement("a");
    link.download = `nubemovil_${state.route ? state.route.id : "ruta"}_${String(state.index + 1).padStart(4, "0")}.png`;
    link.href = canvas.toDataURL("image/png");
    link.click();
    privateStatus.textContent = "PNG exportado.";
  } catch (error) {
    privateStatus.textContent = error.message;
  }
}

async function htmlCanvas() {
  const { width, height, scale } = exportDimensions();
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const s = scale;
  const point = state.points[state.index] || state.points[0];
  const [metricName, metricUnit, metricDigits] = metricLabel(state.metric);
  const metricValue = point ? formatValue(point.measures[state.metric], metricUnit, metricDigits) : "-";
  const portrait = height > width;
  const panelHeight = (portrait ? 560 : 330) * s;
  const mapBox = {
    x: 0,
    y: 0,
    w: width,
    h: height - panelHeight,
  };
  const dataBox = {
    x: 0,
    y: height - panelHeight,
    w: width,
    h: panelHeight,
  };

  ctx.fillStyle = "#f5f8fb";
  ctx.fillRect(0, 0, width, height);

  await drawExportMap(ctx, mapBox, s);
  drawExportPanel(ctx, dataBox, s, point, metricName, metricValue);
  return canvas;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function canvasToPngBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("No se pudo generar el PNG."));
    }, "image/png");
  });
}

async function startRecording() {
  if (!state.points.length) return;
  if (state.index >= state.points.length - 1) updateCurrent(0, false);

  const startIndex = state.index;
  state.recording = true;
  state.recordFrames = [];
  recordPlayButton.textContent = "Detener captura";
  setPlaying(false);

  try {
    for (let index = startIndex; index < state.points.length && state.recording; index += 1) {
      updateCurrent(index, false);
      const canvas = await htmlCanvas();
      const blob = await canvasToPngBlob(canvas);
      state.recordFrames.push({
        name: `frame_${String(state.recordFrames.length + 1).padStart(5, "0")}.png`,
        blob,
      });
      privateStatus.textContent = `Capturando PNG ${state.recordFrames.length}/${state.points.length - startIndex}...`;
      await wait(180);
    }

    const frames = state.recordFrames;
    state.recording = false;
    recordPlayButton.textContent = "Grabar PNGs";
    if (frames.length) await downloadFrameZip(frames);
  } catch (error) {
    privateStatus.textContent = error.message;
    stopRecording();
  }
}

function stopRecording() {
  if (!state.recording) return;
  state.recording = false;
  recordPlayButton.textContent = "Grabar PNGs";
  privateStatus.textContent = state.recordFrames.length
    ? `Captura detenida. Generando ZIP con ${state.recordFrames.length} PNGs...`
    : "Captura detenida.";
}

function toggleRecording() {
  if (state.recording) {
    setPlaying(false);
    stopRecording();
    return;
  }
  startRecording().catch((error) => {
    privateStatus.textContent = error.message;
    stopRecording();
  });
}

function crc32(bytes) {
  let crc = -1;
  for (let i = 0; i < bytes.length; i += 1) {
    crc ^= bytes[i];
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return (crc ^ -1) >>> 0;
}

function uint16(value) {
  return new Uint8Array([value & 255, (value >>> 8) & 255]);
}

function uint32(value) {
  return new Uint8Array([value & 255, (value >>> 8) & 255, (value >>> 16) & 255, (value >>> 24) & 255]);
}

async function downloadFrameZip(frames) {
  privateStatus.textContent = `Generando ZIP con ${frames.length} PNGs...`;
  const encoder = new TextEncoder();
  const chunks = [];
  const central = [];
  let offset = 0;

  for (const frame of frames) {
    const data = new Uint8Array(await frame.blob.arrayBuffer());
    const name = encoder.encode(frame.name);
    const crc = crc32(data);
    const localHeader = [
      uint32(0x04034b50), uint16(20), uint16(0), uint16(0), uint16(0), uint16(0),
      uint32(crc), uint32(data.length), uint32(data.length), uint16(name.length), uint16(0), name,
    ];
    chunks.push(...localHeader, data);
    central.push(
      uint32(0x02014b50), uint16(20), uint16(20), uint16(0), uint16(0), uint16(0), uint16(0),
      uint32(crc), uint32(data.length), uint32(data.length), uint16(name.length), uint16(0),
      uint16(0), uint16(0), uint16(0), uint32(0), uint32(offset), name,
    );
    offset += localHeader.reduce((sum, chunk) => sum + chunk.length, 0) + data.length;
  }

  const centralOffset = offset;
  const centralSize = central.reduce((sum, chunk) => sum + chunk.length, 0);
  chunks.push(...central);
  chunks.push(
    uint32(0x06054b50), uint16(0), uint16(0), uint16(frames.length), uint16(frames.length),
    uint32(centralSize), uint32(centralOffset), uint16(0),
  );

  const blob = new Blob(chunks, { type: "application/zip" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.download = `nubemovil_${state.route ? state.route.id : "ruta"}_pngs.zip`;
  link.href = url;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  state.recordFrames = [];
  privateStatus.textContent = `ZIP exportado con ${frames.length} PNGs.`;
}

function exportTileZoom() {
  const zooms = {
    far: 15,
    normal: 16,
    near: 17,
    detail: 18,
  };
  return zooms[exportZoomSelect.value] ?? zooms.normal;
}

function latLonToWorld(lat, lon, zoom) {
  const sin = Math.sin((lat * Math.PI) / 180);
  const scale = 256 * (2 ** zoom);
  return {
    x: ((lon + 180) / 360) * scale,
    y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale,
  };
}

function routeCenter() {
  const lats = state.points.map((point) => point.gps_lat);
  const lons = state.points.map((point) => point.gps_lon);
  return {
    lat: (Math.min(...lats) + Math.max(...lats)) / 2,
    lon: (Math.min(...lons) + Math.max(...lons)) / 2,
  };
}

function projectedPoints(box, scale) {
  const zoom = exportTileZoom();
  const center = latLonToWorld(routeCenter().lat, routeCenter().lon, zoom);
  const topLeft = {
    x: center.x - (box.w / scale) / 2,
    y: center.y - (box.h / scale) / 2,
  };
  return state.points.map((point) => ({
    x: box.x + (latLonToWorld(point.gps_lat, point.gps_lon, zoom).x - topLeft.x) * scale,
    y: box.y + (latLonToWorld(point.gps_lat, point.gps_lon, zoom).y - topLeft.y) * scale,
  }));
}

function drawRoundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function drawMapContext(ctx, box, scale) {
  ctx.fillStyle = "#eef3f5";
  ctx.fillRect(box.x, box.y, box.w, box.h);

  ctx.fillStyle = "#dfecea";
  ctx.beginPath();
  ctx.ellipse(box.x + box.w * 0.16, box.y + box.h * 0.25, box.w * 0.24, box.h * 0.16, -0.2, 0, Math.PI * 2);
  ctx.fill();
  ctx.beginPath();
  ctx.ellipse(box.x + box.w * 0.78, box.y + box.h * 0.72, box.w * 0.25, box.h * 0.18, 0.35, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#d7e7ef";
  ctx.beginPath();
  ctx.moveTo(box.x + box.w * 0.74, box.y);
  ctx.bezierCurveTo(box.x + box.w * 0.82, box.y + box.h * 0.22, box.x + box.w * 0.83, box.y + box.h * 0.46, box.x + box.w, box.y + box.h * 0.58);
  ctx.lineTo(box.x + box.w, box.y);
  ctx.closePath();
  ctx.fill();

  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  const majorRoads = [
    [[-0.08, 0.25], [0.22, 0.3], [0.52, 0.42], [1.08, 0.54]],
    [[0.04, 0.78], [0.28, 0.67], [0.5, 0.58], [0.96, 0.36]],
    [[0.62, -0.08], [0.58, 0.23], [0.55, 0.52], [0.52, 1.08]],
    [[0.18, -0.06], [0.3, 0.22], [0.37, 0.48], [0.48, 1.06]],
  ];
  majorRoads.forEach((road) => {
    drawMapRoad(ctx, box, road, "#ffffff", 14 * scale);
    drawMapRoad(ctx, box, road, "#cfd9df", 2 * scale);
  });

  for (let i = -2; i < 12; i += 1) {
    const x = i / 10;
    drawMapRoad(ctx, box, [[x, -0.05], [x + 0.08, 0.25], [x + 0.03, 0.58], [x + 0.12, 1.05]], "#ffffff", 7 * scale);
  }
  for (let i = -2; i < 12; i += 1) {
    const y = i / 10;
    drawMapRoad(ctx, box, [[-0.05, y], [0.24, y + 0.04], [0.58, y - 0.02], [1.05, y + 0.03]], "#ffffff", 7 * scale);
  }

  ctx.fillStyle = "rgba(99, 114, 127, 0.42)";
  ctx.font = `600 ${18 * scale}px Inter, Arial, sans-serif`;
  ctx.fillText("Aviles", box.x + box.w * 0.08, box.y + box.h * 0.14);
  ctx.fillText("Parque", box.x + box.w * 0.74, box.y + box.h * 0.78);
  ctx.fillText("Ria", box.x + box.w * 0.86, box.y + box.h * 0.24);
}

function drawMapRoad(ctx, box, points, color, width) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  points.forEach(([px, py], index) => {
    const x = box.x + box.w * px;
    const y = box.y + box.h * py;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function loadTile(url) {
  if (tileCache.has(url)) return tileCache.get(url);
  const promise = new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });
  tileCache.set(url, promise);
  return promise;
}

function exportTileUrl(zoom, x, y) {
  if (exportMapSelect.value === "osm") {
    return `https://tile.openstreetmap.org/${zoom}/${x}/${y}.png`;
  }
  const subdomains = ["a", "b", "c", "d"];
  const subdomain = subdomains[Math.abs(x + y) % subdomains.length];
  return `https://${subdomain}.basemaps.cartocdn.com/light_all/${zoom}/${x}/${y}.png`;
}

async function drawLiveTiles(ctx, box, scale) {
  const zoom = exportTileZoom();
  const center = latLonToWorld(routeCenter().lat, routeCenter().lon, zoom);
  const worldW = box.w / scale;
  const worldH = box.h / scale;
  const topLeft = {
    x: center.x - worldW / 2,
    y: center.y - worldH / 2,
  };
  const minTileX = Math.floor(topLeft.x / 256);
  const maxTileX = Math.floor((topLeft.x + worldW) / 256);
  const minTileY = Math.floor(topLeft.y / 256);
  const maxTileY = Math.floor((topLeft.y + worldH) / 256);
  const maxTile = (2 ** zoom) - 1;
  const tiles = [];

  for (let tileY = minTileY; tileY <= maxTileY; tileY += 1) {
    for (let tileX = minTileX; tileX <= maxTileX; tileX += 1) {
      if (tileY < 0 || tileY > maxTile) continue;
      const wrappedX = ((tileX % (maxTile + 1)) + (maxTile + 1)) % (maxTile + 1);
      const url = exportTileUrl(zoom, wrappedX, tileY);
      tiles.push(loadTile(url).then((img) => ({ img, tileX, tileY })));
    }
  }

  const loaded = await Promise.all(tiles);
  loaded.forEach(({ img, tileX, tileY }) => {
    const x = box.x + ((tileX * 256) - topLeft.x) * scale;
    const y = box.y + ((tileY * 256) - topLeft.y) * scale;
    ctx.drawImage(img, x, y, 256 * scale, 256 * scale);
  });
}

async function drawExportMap(ctx, box, scale) {
  const coords = projectedPoints(box, scale);
  const range = metricRange(state.metric);
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.x, box.y, box.w, box.h);
  ctx.clip();
  drawMapContext(ctx, box, scale);
  try {
    await drawLiveTiles(ctx, box, scale);
  } catch (_) {
    drawMapContext(ctx, box, scale);
  }

  drawPath(ctx, coords, "#263238", 7 * scale, 0.28);
  drawPath(ctx, coords.slice(0, state.index + 1), "#58bdf0", 8 * scale, 0.95);
  coords.forEach((coord, index) => {
    const color = colorFor(numberValue(state.points[index].measures[state.metric]), range);
    ctx.beginPath();
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.9;
    ctx.arc(coord.x, coord.y, 8 * scale, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.globalAlpha = 1;

  const current = coords[state.index] || coords[0];
  if (current) drawExportCurrentMarker(ctx, current, scale);
  ctx.restore();
}

function drawPath(ctx, coords, color, width, alpha) {
  if (coords.length < 2) return;
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(coords[0].x, coords[0].y);
  coords.slice(1).forEach((coord) => ctx.lineTo(coord.x, coord.y));
  ctx.stroke();
  ctx.restore();
}

function drawExportCurrentMarker(ctx, coord, scale) {
  const point = state.points[state.index];
  ctx.save();
  ctx.shadowColor = "rgba(22, 31, 41, 0.22)";
  ctx.shadowBlur = 24 * scale;
  ctx.shadowOffsetY = 12 * scale;
  if (useTemperatureEmoji && isTemperatureMetric(state.metric)) {
    ctx.beginPath();
    ctx.fillStyle = "rgba(255, 255, 255, 0.96)";
    ctx.arc(coord.x, coord.y, 24 * scale, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.font = `${34 * scale}px Arial, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(emojiForTemperature(point.measures[state.metric]), coord.x, coord.y + 1 * scale);
  } else {
    ctx.beginPath();
    ctx.fillStyle = "#58bdf0";
    ctx.arc(coord.x, coord.y, 20 * scale, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.lineWidth = 4 * scale;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  }
  ctx.restore();
}

function currentDataRows(point) {
  if (!point) return [];
  const rows = [
    ["Ruta", state.route ? state.route.title : "-"],
    ["Hora", formatDateTime(point.captured_at_iso)],
    ["Altitud", formatValue(point.gps_alt, "m", 1)],
    ["Velocidad", formatValue(point.measures.gps_speed_kmh, "km/h", 1)],
    ["Lat / Lon", `${point.gps_lat.toFixed(6)}, ${point.gps_lon.toFixed(6)}`],
  ];
  const orderedMetrics = state.metrics
    .filter((key) => !shouldHideDataRow(key, point.measures[key]))
    .sort((a, b) => {
      const ai = exportMetricOrder.indexOf(a);
      const bi = exportMetricOrder.indexOf(b);
      if (ai === -1 && bi === -1) return state.metrics.indexOf(a) - state.metrics.indexOf(b);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    })
    .slice(0, 16);
  return rows.concat(orderedMetrics.map((key) => {
      const [label, unit, digits] = metricLabel(key);
      return [label, formatValue(point.measures[key], unit, digits)];
    }));
}

function drawExportPanel(ctx, box, scale, point, metricName, metricValue) {
  ctx.beginPath();
  ctx.rect(box.x, box.y, box.w, box.h);
  ctx.fillStyle = "#07131f";
  ctx.fill();
  ctx.strokeStyle = "#58bdf0";
  ctx.lineWidth = 3 * scale;
  ctx.beginPath();
  ctx.moveTo(box.x, box.y);
  ctx.lineTo(box.x + box.w, box.y);
  ctx.stroke();

  const inset = 48 * scale;
  ctx.fillStyle = "#ffffff";
  ctx.font = `italic 800 ${34 * scale}px Inter, Arial, sans-serif`;
  ctx.fillText("NUBEMOVIL", box.x + inset, box.y + 56 * scale);
  ctx.fillStyle = "rgba(255, 255, 255, 0.72)";
  ctx.font = `800 ${16 * scale}px Inter, Arial, sans-serif`;
  ctx.fillText("by ROTOR STUDIO", box.x + inset, box.y + 86 * scale);

  ctx.textAlign = "right";
  ctx.fillStyle = "#58bdf0";
  ctx.font = `800 ${20 * scale}px Inter, Arial, sans-serif`;
  ctx.fillText(metricName, box.x + box.w - inset, box.y + 50 * scale);
  ctx.fillStyle = "#ffffff";
  ctx.font = `800 ${34 * scale}px Inter, Arial, sans-serif`;
  ctx.fillText(metricValue, box.x + box.w - inset, box.y + 88 * scale);
  ctx.textAlign = "left";

  const rows = currentDataRows(point);
  const ratio = box.w / box.h;
  const columns = ratio > 4.5 ? 5 : (ratio > 2.6 ? 4 : (ratio > 1.35 ? 3 : 2));
  const colW = (box.w - inset * 2) / columns;
  const rowGap = 48 * scale;
  const firstRowY = 126 * scale;
  rows.forEach(([label, value], index) => {
    const col = index % columns;
    const row = Math.floor(index / columns);
    const x = box.x + inset + col * colW;
    const y = box.y + firstRowY + row * rowGap;
    ctx.fillStyle = "rgba(255, 255, 255, 0.58)";
    ctx.font = `700 ${18 * scale}px Inter, Arial, sans-serif`;
    ctx.fillText(label, x, y);
    ctx.fillStyle = "#ffffff";
    ctx.font = `800 ${26 * scale}px Inter, Arial, sans-serif`;
    ctx.fillText(String(value), x, y + 30 * scale);
  });
}

routeSelect.addEventListener("change", () => {
  if (state.recording) stopRecording();
  loadRoute(routeSelect.value);
});
metricSelect.addEventListener("change", () => {
  state.metric = metricSelect.value;
  renderMetric();
  updateEmojiToggle();
  updateCurrent(state.index, false);
});
emojiToggle.addEventListener("click", () => {
  if (!isTemperatureMetric(state.metric)) return;
  useTemperatureEmoji = !useTemperatureEmoji;
  updateEmojiToggle();
});
timeline.addEventListener("input", () => {
  setPlaying(false);
  updateCurrent(Number(timeline.value));
});
csvInput.addEventListener("change", () => {
  csvFileName.textContent = csvInput.files.length ? csvInput.files[0].name : "Ningun archivo seleccionado";
});
pickCsvButton.addEventListener("click", () => {
  csvInput.click();
});
playButton.addEventListener("click", () => {
  if (state.index >= state.points.length - 1) updateCurrent(0, false);
  setPlaying(!state.playing);
});
playSpeedSelect.addEventListener("change", () => {
  if (state.playing) setPlaying(true);
});
loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = await api("/?api=login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: passwordInput.value }),
    });
    state.csrf = payload.csrf;
    passwordInput.value = "";
    privateStatus.textContent = "Sesion privada activa.";
    await loadRoutes();
  } catch (error) {
    privateStatus.textContent = error.message;
  }
});
logoutButton.addEventListener("click", async () => {
  await api("/?api=logout", { method: "POST", headers: { "X-CSRF-Token": state.csrf } });
  await loadRoutes();
});
renameRouteButton.addEventListener("click", async () => {
  if (!state.route) return;
  const title = routeRenameInput.value.trim();
  if (!title) {
    privateStatus.textContent = "Escribe un nombre para la ruta.";
    return;
  }
  try {
    privateStatus.textContent = "Renombrando...";
    await api("/?api=renameRoute", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ id: state.route.id, title }),
    });
    state.route.title = title;
    privateStatus.textContent = "Ruta renombrada.";
    await loadRoutes();
  } catch (error) {
    privateStatus.textContent = error.message;
  }
});
deleteRouteButton.addEventListener("click", async () => {
  if (!state.route) return;
  const confirmed = window.confirm(`Borrar "${state.route.title}"? Esta accion no se puede deshacer.`);
  if (!confirmed) return;
  try {
    privateStatus.textContent = "Borrando...";
    await api("/?api=deleteRoute", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ id: state.route.id }),
    });
    state.route = null;
    privateStatus.textContent = "Ruta borrada.";
    await loadRoutes();
  } catch (error) {
    privateStatus.textContent = error.message;
  }
});
uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!csvInput.files.length) {
    privateStatus.textContent = "Selecciona un CSV para subir la ruta.";
    csvInput.click();
    return;
  }
  const form = new FormData();
  form.append("title", routeTitleInput.value);
  form.append("csv", csvInput.files[0]);
  if (publicInput.checked) form.append("is_public", "1");
  try {
    privateStatus.textContent = "Subiendo...";
    await api("/?api=upload", { method: "POST", headers: { "X-CSRF-Token": state.csrf }, body: form });
    routeTitleInput.value = "";
    csvInput.value = "";
    csvFileName.textContent = "Ningun archivo seleccionado";
    privateStatus.textContent = "Ruta subida.";
    await loadRoutes();
  } catch (error) {
    privateStatus.textContent = error.message;
  }
});
exportPngButton.addEventListener("click", exportPng);
recordPlayButton.addEventListener("click", toggleRecording);

loadRoutes().catch((error) => {
  dataPanel.innerHTML = `<div class="data-row"><span>${escapeHtml(error.message)}</span></div>`;
});
