<?php

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$state = read_state();
$siteName = app_config()['site_name'];
$frameRefreshMs = (int) app_config()['frame_refresh_ms'];
$stateRefreshMs = (int) app_config()['state_refresh_ms'];
$display = is_array($state['display'] ?? null) ? $state['display'] : [];
?>
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?= htmlspecialchars($siteName, ENT_QUOTES, 'UTF-8') ?></title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  >
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;700&display=swap');

    :root {
      --white: #ffffff;
      --soft: rgba(255, 255, 255, 0.78);
      --faint: rgba(255, 255, 255, 0.48);
      --line: rgba(255, 255, 255, 0.16);
      --shell-pad-x: 30px;
      --shell-pad-y: 28px;
      --brand-mark-size: 51px;
      --brand-gap: 16px;
      --sidebar-w: 280px;
      --sidebar-gap: 36px;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      overflow: hidden;
      font-family: "Space Grotesk", "Segoe UI", sans-serif;
      color: var(--white);
      background: #091018;
    }

    .background {
      position: fixed;
      inset: 0;
      overflow: hidden;
    }

    .background-layer {
      position: absolute;
      inset: 0;
      background:
        linear-gradient(rgba(6, 10, 16, 0.08), rgba(6, 10, 16, 0.26)),
        center center / cover no-repeat;
      transform: scale(1.05);
      transition: opacity 0.9s ease;
      opacity: 0;
    }

    .background-layer.is-visible {
      opacity: 1;
    }

    .background::after {
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at 50% 44%, rgba(255,255,255,0.04), transparent 34%),
        linear-gradient(90deg, rgba(6, 10, 16, 0.22) 0%, rgba(6, 10, 16, 0.04) 38%, rgba(6, 10, 16, 0.26) 100%);
    }

    .shell {
      position: relative;
      z-index: 1;
      min-height: 100vh;
      padding: var(--shell-pad-y) var(--shell-pad-x) 34px;
    }

    .masthead {
      display: flex;
      align-items: center;
      gap: 16px;
      min-height: 68px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
    }

    .brand-mark {
      width: var(--brand-mark-size);
      height: var(--brand-mark-size);
      flex: 0 0 var(--brand-mark-size);
    }

    .brand-copy {
      margin: 0;
      font-size: clamp(16px, 1.35vw, 20px);
      line-height: 1.1;
      font-weight: 400;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--soft);
    }

    .layout {
      min-height: calc(100vh - 96px);
    }

    .sidebar {
      position: absolute;
      top: calc(var(--shell-pad-y) + 2px);
      right: var(--shell-pad-x);
      width: min(var(--sidebar-w), 100%);
      height: calc(100vh - (var(--shell-pad-y) * 2) - 2px);
      padding-bottom: 0;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }

    .metrics {
      display: flex;
      flex-direction: column;
      gap: 12px;
      flex: 1 1 auto;
      justify-content: space-evenly;
    }

    .metric {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--line);
      text-align: right;
    }

    .metric-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }

    .metric-row .metric {
      border-bottom: 0;
      padding-bottom: 0;
    }

    .metric-label {
      font-size: 10px;
      line-height: 1.2;
      font-weight: 400;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--faint);
    }

    .metric-value {
      font-size: clamp(18px, 1.75vw, 24px);
      line-height: 1;
      font-weight: 400;
      color: var(--soft);
    }

    .metric-value-unit {
      margin-left: 6px;
      font-size: 11px;
      line-height: 1;
      font-weight: 400;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--faint);
    }

    .meta {
      margin-top: 10px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 10px;
      line-height: 1.4;
      font-weight: 400;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--faint);
      text-align: right;
    }

    .map-wrap {
      position: relative;
      margin-top: 12px;
      padding-top: 0;
    }

    .map-frame {
      position: relative;
      width: 100%;
      height: 114px;
      overflow: hidden;
      background: rgba(0, 0, 0, 0.16);
    }

    .map-frame #mapFrame {
      width: 100%;
      height: 100%;
      filter: grayscale(1) contrast(1.02) brightness(0.78);
    }

    .map-overlay {
      position: absolute;
      inset: 0;
      background: linear-gradient(rgba(7, 11, 16, 0.18), rgba(7, 11, 16, 0.18));
      pointer-events: none;
      z-index: 450;
    }

    .map-coords {
      margin-top: 6px;
      font-size: 10px;
      line-height: 1.35;
      font-weight: 400;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--faint);
      text-align: right;
      word-break: break-word;
    }

    .updated {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }

    .board {
      position: absolute;
      top: 0;
      left: 0;
      right: calc(var(--sidebar-w) + var(--sidebar-gap));
      bottom: 0;
      display: flex;
      align-items: flex-end;
      justify-content: flex-start;
      padding: 8vh var(--shell-pad-x) 84px calc(var(--shell-pad-x) + var(--brand-mark-size) + var(--brand-gap));
      text-align: left;
    }

    .board-copy {
      max-width: min(980px, 72vw);
      width: 100%;
    }

    .headline {
      margin: 0;
      font-size: clamp(18px, 2.1vw, 30px);
      line-height: 1.1;
      font-weight: 400;
      letter-spacing: 0.08em;
      text-transform: none;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--white);
      text-shadow: 0 12px 40px rgba(0, 0, 0, 0.34);
    }

    .line {
      margin: 18px auto 0;
      max-width: 42rem;
      font-size: clamp(16px, 2.2vw, 28px);
      line-height: 1.25;
      font-weight: 700;
      color: rgba(255, 255, 255, 0.92);
      text-wrap: balance;
    }

    .line.secondary {
      margin-top: 10px;
      font-size: clamp(14px, 1.6vw, 20px);
      font-weight: 400;
      color: var(--soft);
    }

    .board .line,
    .board .line.secondary {
      display: none;
    }

    @media (max-width: 900px) {
      body {
        overflow-x: hidden;
        overflow-y: auto;
      }

      .shell {
        padding: 18px 18px 24px;
        min-height: auto;
      }

      .layout {
        position: static;
        min-height: auto;
      }

      .sidebar {
        position: static;
        width: 100%;
        max-width: none;
        margin-top: 24px;
        height: auto;
        display: block;
      }

      .metrics {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 18px 18px;
        justify-content: stretch;
        flex: none;
      }

      .metric {
        padding-bottom: 10px;
        text-align: left;
      }

      .metric-row {
        grid-template-columns: 1fr;
      }

      .meta {
        text-align: left;
      }

      .board {
        position: static;
        min-height: auto;
        padding: 12vw 4vw 4vw;
        align-items: center;
        justify-content: center;
        text-align: center;
        right: auto;
      }

      .board-copy {
        max-width: none;
      }

      .headline {
        white-space: normal;
      }
    }

    @media (max-width: 620px) {
      .brand-mark {
        width: 40px;
        height: 40px;
        flex-basis: 40px;
      }

      .brand-copy {
        font-size: 19px;
      }

      .metrics {
        grid-template-columns: 1fr;
      }

      .board {
        padding: 14vw 2vw 6vw;
      }

      .map-coords,
      .meta {
        text-align: left;
      }
    }
  </style>
</head>
<body>
  <div class="background" id="background">
    <div class="background-layer is-visible" id="bgLayerA" style="background-image: linear-gradient(rgba(6, 10, 16, 0.08), rgba(6, 10, 16, 0.26)), url('cloud_frame.php');"></div>
    <div class="background-layer" id="bgLayerB"></div>
  </div>
  <div class="shell">
    <header class="masthead">
      <div class="brand">
        <img
          class="brand-mark"
          src="static/logoRotor-150x150.png"
          alt="Rotor Studio"
          onerror="this.style.display='none'; this.nextElementSibling.style.display='block';"
        >
        <svg class="brand-mark" viewBox="0 0 128 128" aria-hidden="true" style="display:none;">
          <circle cx="64" cy="64" r="61" fill="#f7f7f7"></circle>
          <path d="M45 100.5V28.5h4.7v72z" fill="#111111"></path>
          <path d="M51.5 35.5c9.2-4.5 23.9-7.2 42.3-4.8l10.8 30.4c-16.4-3.3-31.1-1.3-43.2 4.3L51.5 35.5z" fill="#111111"></path>
        </svg>
        <p class="brand-copy">NUBEMOVIL - ROTOR STUDIO</p>
      </div>
    </header>

    <main class="layout">
      <section class="board">
        <div class="board-copy">
          <h1 class="headline" id="headline"><?= htmlspecialchars((string) ($display['headline'] ?? $state['subtitle'] ?? 'NUBEMOVIL'), ENT_QUOTES, 'UTF-8') ?></h1>
          <p class="line" id="line1"><?= htmlspecialchars((string) ($display['line1'] ?? ''), ENT_QUOTES, 'UTF-8') ?></p>
          <p class="line secondary" id="line2"><?= htmlspecialchars((string) ($display['line2'] ?? ''), ENT_QUOTES, 'UTF-8') ?></p>
        </div>
      </section>

      <aside class="sidebar">
        <section class="metrics" id="metrics"></section>
        <div class="map-wrap">
          <div class="map-frame">
            <div id="mapFrame" aria-hidden="true"></div>
            <div class="map-overlay"></div>
          </div>
          <div class="map-coords" id="mapCoords"><?= htmlspecialchars((string) ($state['location'] ?? 'Sin ubicacion'), ENT_QUOTES, 'UTF-8') ?></div>
        </div>
        <div class="meta updated">
          <span id="updatedAt"><?= htmlspecialchars((string) ($state['updated_at'] ?? 'Sin actualizacion'), ENT_QUOTES, 'UTF-8') ?></span>
        </div>
      </aside>
    </main>
  </div>

  <script>
    const frameRefreshMs = <?= $frameRefreshMs ?>;
    const stateRefreshMs = <?= $stateRefreshMs ?>;
    const onlineThresholdMs = stateRefreshMs * 3;
    let activeBackgroundLayer = 'A';
    let lastUpdatedAtValue = <?= json_encode((string) ($state['updated_at'] ?? ''), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>;
    let latestOnlineFlag = null;

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function renderMetrics(metrics) {
      const host = document.getElementById('metrics');
      if (!host) return;

      if (!Array.isArray(metrics) || metrics.length === 0) {
        host.innerHTML = '<article class="metric"><div class="metric-label">Sin datos</div><div class="metric-value">--</div></article>';
        return;
      }

      const uniqueMetrics = [];
      const seenLabels = new Set();
      metrics.forEach((metric) => {
        const label = String(metric.label || '').trim();
        if (!label || seenLabels.has(label)) return;
        seenLabels.add(label);
        uniqueMetrics.push(metric);
      });

      const byLabel = new Map(uniqueMetrics.map((metric) => [String(metric.label || ''), metric]));
      const ordered = [];
      const consumed = new Set();

      function renderMetric(metric) {
        return `
          <article class="metric">
            <div class="metric-label">${escapeHtml(metric.label || metric.metric_id || 'Dato')}</div>
            <div class="metric-value">
              ${escapeHtml(metric.value ?? '--')}
              ${metric.unit ? `<span class="metric-value-unit">${escapeHtml(metric.unit)}</span>` : ''}
            </div>
          </article>
        `;
      }

      function pushSingle(label) {
        const metric = byLabel.get(label);
        if (!metric) return;
        consumed.add(label);
        ordered.push(renderMetric(metric));
      }

      function pushPair(labelA, labelB) {
        const metricA = byLabel.get(labelA);
        const metricB = byLabel.get(labelB);
        if (!metricA && !metricB) return;
        consumed.add(labelA);
        consumed.add(labelB);
        ordered.push(`
          <div class="metric-row">
            ${metricA ? renderMetric(metricA) : '<article class="metric"></article>'}
            ${metricB ? renderMetric(metricB) : '<article class="metric"></article>'}
          </div>
        `);
      }

      function pushCustomPair(leftHtml, rightHtml) {
        ordered.push(`
          <div class="metric-row">
            ${leftHtml}
            ${rightHtml}
          </div>
        `);
      }

      pushSingle('Direccion');
      pushSingle('Velocidad');
      pushSingle('Luminosidad');
      pushSingle('Radiacion UV');
      pushSingle('Lluvia');
      pushPair('Temp', 'Temp suelo');
      pushPair('Humedad', 'Hum suelo');
      pushPair('PM2.5', 'PM10');
      const voltajeMetric = byLabel.get('Voltaje');
      if (voltajeMetric) {
        consumed.add('Voltaje');
      }
      pushCustomPair(
        voltajeMetric ? renderMetric(voltajeMetric) : '<article class="metric"></article>',
        `
          <article class="metric">
            <div class="metric-label">Estado</div>
            <div class="metric-value" id="onlineStatus">Online</div>
          </article>
        `
      );

      uniqueMetrics.forEach((metric) => {
        const label = String(metric.label || '');
        if (consumed.has(label)) return;
        ordered.push(renderMetric(metric));
      });

      host.innerHTML = ordered.join('');
    }

    function renderDisplay(payload) {
      const display = payload?.display || {};
      const parts = [display.headline, display.line1, display.line2]
        .map((value) => String(value || '').trim())
        .filter(Boolean);
      const headline = parts.length ? parts.join('. ') : (payload.subtitle || 'NUBEMOVIL');

      document.getElementById('headline').textContent = headline;
      document.getElementById('line1').textContent = '';
      document.getElementById('line1').style.display = 'none';
      document.getElementById('line2').textContent = '';
      document.getElementById('line2').style.display = 'none';
    }

    function computeOnlineStatus(updatedAt, onlineFlag = null) {
      if (typeof onlineFlag === 'boolean') {
        return onlineFlag ? 'Online' : 'Offline';
      }
      if (!updatedAt) return 'Offline';
      const ts = Date.parse(updatedAt);
      if (Number.isNaN(ts)) return 'Offline';
      const ageMs = Date.now() - ts;
      return ageMs <= onlineThresholdMs ? 'Online' : 'Offline';
    }

    function parseLocation(location) {
      const raw = String(location || '').trim();
      const match = raw.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
      if (!match) return null;
      return {
        lat: Number(match[1]),
        lon: Number(match[2]),
      };
    }

    let leafletMap = null;
    let leafletMarker = null;

    function renderMap(location) {
      const label = document.getElementById('mapCoords');
      const frame = document.getElementById('mapFrame');
      if (label) {
        label.textContent = location || 'Sin ubicacion';
      }
      if (!frame) return;

      const coords = parseLocation(location);
      if (!coords) {
        return;
      }

      if (!window.L) {
        return;
      }

      if (!leafletMap) {
        leafletMap = window.L.map(frame, {
          zoomControl: false,
          attributionControl: false,
          dragging: false,
          scrollWheelZoom: false,
          doubleClickZoom: false,
          boxZoom: false,
          keyboard: false,
          tap: false,
          touchZoom: false,
        });

        window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
          maxZoom: 19,
        }).addTo(leafletMap);

        const markerIcon = window.L.divIcon({
          className: 'custom-marker',
          html: '<div style="width:12px;height:12px;border-radius:999px;background:#ffffff;border:2px solid rgba(9,16,24,0.8);box-shadow:0 0 0 8px rgba(255,255,255,0.12);"></div>',
          iconSize: [12, 12],
          iconAnchor: [6, 6],
        });

        leafletMarker = window.L.marker([coords.lat, coords.lon], { icon: markerIcon }).addTo(leafletMap);
      } else {
        leafletMarker.setLatLng([coords.lat, coords.lon]);
      }

      leafletMap.setView([coords.lat, coords.lon], 13);
      setTimeout(() => leafletMap.invalidateSize(), 50);
    }

    function renderOnlineStatus(updatedAt, onlineFlag = null) {
      const statusEl = document.getElementById('onlineStatus');
      if (!statusEl) return;
      statusEl.textContent = computeOnlineStatus(updatedAt, onlineFlag);
    }

    async function refreshState() {
      try {
        const response = await fetch(`cloud_state.php?t=${Date.now()}`, { cache: 'no-store' });
        const payload = await response.json();

        lastUpdatedAtValue = payload.updated_at || '';
        latestOnlineFlag = typeof payload.is_online === 'boolean' ? payload.is_online : null;
        renderMetrics(payload.metrics || []);
        renderOnlineStatus(lastUpdatedAtValue, latestOnlineFlag);
        document.getElementById('updatedAt').textContent = payload.updated_at ? `Actualizado ${payload.updated_at}` : 'Sin actualizacion';
        renderDisplay(payload);
        renderMap(payload.location || '');
      } catch (error) {
        latestOnlineFlag = false;
        renderOnlineStatus('');
        document.getElementById('updatedAt').textContent = 'Sin actualizacion';
      }
    }

    function heartbeatOnlineStatus() {
      if (latestOnlineFlag === true && lastUpdatedAtValue) {
        const ts = Date.parse(lastUpdatedAtValue);
        if (!Number.isNaN(ts) && (Date.now() - ts) > onlineThresholdMs) {
          latestOnlineFlag = false;
        }
      }
      renderOnlineStatus(lastUpdatedAtValue, latestOnlineFlag);
    }

    function refreshFrame() {
      const currentLayer = document.getElementById(activeBackgroundLayer === 'A' ? 'bgLayerA' : 'bgLayerB');
      const nextLayer = document.getElementById(activeBackgroundLayer === 'A' ? 'bgLayerB' : 'bgLayerA');
      if (!currentLayer || !nextLayer) return;

      const nextUrl = `cloud_frame.php?t=${Date.now()}`;
      const preloader = new Image();
      preloader.onload = () => {
        nextLayer.style.backgroundImage =
          `linear-gradient(rgba(6, 10, 16, 0.08), rgba(6, 10, 16, 0.26)), url("${nextUrl}")`;
        nextLayer.classList.add('is-visible');
        currentLayer.classList.remove('is-visible');
        activeBackgroundLayer = activeBackgroundLayer === 'A' ? 'B' : 'A';
      };
      preloader.src = nextUrl;
    }

    renderMetrics(<?= json_encode($state['metrics'] ?? [], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>);
    latestOnlineFlag = <?= json_encode(state_is_online($state)) ?>;
    renderOnlineStatus(lastUpdatedAtValue, latestOnlineFlag);
    renderDisplay(<?= json_encode($state, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>);
    renderMap(<?= json_encode((string) ($state['location'] ?? ''), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>);
    refreshState();
    refreshFrame();

    setInterval(refreshState, stateRefreshMs);
    setInterval(refreshFrame, frameRefreshMs);
    setInterval(heartbeatOnlineStatus, 1000);
  </script>
</body>
<script
  src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
  crossorigin=""
></script>
</html>
