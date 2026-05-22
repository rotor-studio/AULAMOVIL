<?php

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$state = read_state();
$siteName = app_config()['site_name'];
$frameRefreshMs = (int) app_config()['frame_refresh_ms'];
$stateRefreshMs = (int) app_config()['state_refresh_ms'];
?>
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?= htmlspecialchars($siteName, ENT_QUOTES, 'UTF-8') ?></title>
  <style>
    :root {
      --sand: #ece5d8;
      --ink: #12212d;
      --panel: rgba(245, 241, 235, 0.78);
      --line: rgba(18, 33, 45, 0.12);
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.22);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--sand);
      background: #0b1117;
      overflow: hidden;
    }

    .background {
      position: fixed;
      inset: 0;
      background:
        linear-gradient(rgba(7, 12, 18, 0.28), rgba(7, 12, 18, 0.48)),
        url('api/frame.php') center center / cover no-repeat;
      transform: scale(1.04);
      transition: background-image 0.6s ease, transform 6s ease;
    }

    .background::after {
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at center, rgba(255,255,255,0.04), transparent 45%),
        linear-gradient(180deg, rgba(9, 15, 23, 0.2), rgba(9, 15, 23, 0.58));
    }

    .wrap {
      position: relative;
      z-index: 1;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 32px;
    }

    .panel {
      width: min(980px, 100%);
      background: var(--panel);
      color: var(--ink);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      padding: clamp(24px, 4vw, 44px);
    }

    .eyebrow {
      margin: 0 0 8px;
      font: 700 12px/1.2 Arial, sans-serif;
      letter-spacing: 0.24em;
      text-transform: uppercase;
      opacity: 0.68;
    }

    h1 {
      margin: 0;
      font-size: clamp(52px, 11vw, 112px);
      line-height: 0.92;
      letter-spacing: -0.05em;
      font-weight: 700;
    }

    .subtitle {
      margin: 14px 0 0;
      max-width: 40rem;
      font: 500 clamp(16px, 2vw, 22px)/1.4 Arial, sans-serif;
      opacity: 0.8;
    }

    .meta {
      margin-top: 20px;
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      font: 600 13px/1.4 Arial, sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      opacity: 0.72;
    }

    .metrics {
      margin-top: 34px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }

    .metric {
      padding: 14px 16px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.46);
      min-height: 104px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }

    .metric-label {
      font: 700 12px/1.3 Arial, sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      opacity: 0.66;
    }

    .metric-value {
      margin-top: 8px;
      font-size: clamp(28px, 4vw, 40px);
      line-height: 1;
      font-weight: 700;
    }

    .metric-unit {
      font: 600 12px/1.2 Arial, sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      opacity: 0.7;
    }

    .status {
      margin-top: 16px;
      font: 600 12px/1.4 Arial, sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      opacity: 0.65;
    }

    @media (max-width: 700px) {
      .wrap { padding: 16px; }
      .panel { padding: 22px; }
    }
  </style>
</head>
<body>
  <div class="background" id="background"></div>
  <div class="wrap">
    <main class="panel">
      <p class="eyebrow">Aula Movil</p>
      <h1 id="title"><?= htmlspecialchars((string) ($state['title'] ?? $siteName), ENT_QUOTES, 'UTF-8') ?></h1>
      <p class="subtitle" id="subtitle"><?= htmlspecialchars((string) ($state['subtitle'] ?? 'Esperando datos'), ENT_QUOTES, 'UTF-8') ?></p>
      <div class="meta">
        <span id="location"><?= htmlspecialchars((string) ($state['location'] ?? 'Sin ubicación'), ENT_QUOTES, 'UTF-8') ?></span>
        <span id="updatedAt"><?= htmlspecialchars((string) ($state['updated_at'] ?? 'Sin actualización'), ENT_QUOTES, 'UTF-8') ?></span>
      </div>
      <section class="metrics" id="metrics"></section>
      <div class="status" id="statusLine">Conectando...</div>
    </main>
  </div>

  <script>
    const frameRefreshMs = <?= $frameRefreshMs ?>;
    const stateRefreshMs = <?= $stateRefreshMs ?>;

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
        host.innerHTML = '<div class="metric"><div class="metric-label">Sin datos</div><div class="metric-value">--</div><div class="metric-unit">Esperando</div></div>';
        return;
      }

      host.innerHTML = metrics.map((metric) => `
        <article class="metric">
          <div class="metric-label">${escapeHtml(metric.label || metric.metric_id || 'Dato')}</div>
          <div class="metric-value">${escapeHtml(metric.value ?? '--')}</div>
          <div class="metric-unit">${escapeHtml(metric.unit || metric.device || '')}</div>
        </article>
      `).join('');
    }

    async function refreshState() {
      const status = document.getElementById('statusLine');

      try {
        const response = await fetch(`api/state.php?t=${Date.now()}`, { cache: 'no-store' });
        const payload = await response.json();

        document.getElementById('title').textContent = payload.title || 'MOVIL CLOUD';
        document.getElementById('subtitle').textContent = payload.subtitle || 'Esperando datos';
        document.getElementById('location').textContent = payload.location || 'Sin ubicación';
        document.getElementById('updatedAt').textContent = payload.updated_at ? `Actualizado ${payload.updated_at}` : 'Sin actualización';
        renderMetrics(payload.metrics || []);

        if (status) {
          status.textContent = payload.camera?.has_frame
            ? 'Último frame recibido correctamente'
            : 'Aún no ha llegado ninguna imagen';
        }
      } catch (error) {
        if (status) {
          status.textContent = 'No se pudo leer el estado local';
        }
      }
    }

    function refreshFrame() {
      const background = document.getElementById('background');
      if (!background) return;
      background.style.backgroundImage =
        `linear-gradient(rgba(7, 12, 18, 0.28), rgba(7, 12, 18, 0.48)), url("api/frame.php?t=${Date.now()}")`;
    }

    renderMetrics(<?= json_encode($state['metrics'] ?? [], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>);
    refreshState();
    refreshFrame();

    setInterval(refreshState, stateRefreshMs);
    setInterval(refreshFrame, frameRefreshMs);
  </script>
</body>
</html>
