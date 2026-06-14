<?php
declare(strict_types=1);

const APP_NAME = 'NUBEMOVIL Player';
const MAX_UPLOAD_BYTES = 8_000_000;

$rootDir = basename(__DIR__) === 'public' ? dirname(__DIR__) : __DIR__;
$varDir = $rootDir . '/var';
$dbPath = $varDir . '/player.sqlite';

if (!is_dir($varDir)) {
    mkdir($varDir, 0750, true);
}

ini_set('session.cookie_httponly', '1');
ini_set('session.cookie_samesite', 'Lax');
if (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
    ini_set('session.cookie_secure', '1');
}
session_start();

function db(): PDO
{
    static $pdo = null;
    global $dbPath;
    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $pdo = new PDO('sqlite:' . $dbPath, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    $pdo->exec('PRAGMA foreign_keys = ON');
    $pdo->exec('PRAGMA journal_mode = WAL');
    init_schema($pdo);
    return $pdo;
}

function init_schema(PDO $pdo): void
{
    $pdo->exec(
        'CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source_filename TEXT,
            is_public INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            point_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            ended_at TEXT
        )'
    );
    $pdo->exec(
        'CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            captured_at_iso TEXT NOT NULL,
            captured_at_epoch REAL,
            gps_lat REAL NOT NULL,
            gps_lon REAL NOT NULL,
            gps_alt REAL,
            measures_json TEXT NOT NULL,
            UNIQUE(route_id, seq)
        )'
    );
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_points_route_seq ON points(route_id, seq)');
}

function json_response(mixed $payload, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

function request_method(): string
{
    return strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
}

function is_private(): bool
{
    return !empty($_SESSION['player_private']);
}

function csrf_token(): string
{
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

function require_private(): void
{
    if (!is_private()) {
        json_response(['error' => 'No autorizado'], 401);
    }
}

function require_csrf(): void
{
    $header = $_SERVER['HTTP_X_CSRF_TOKEN'] ?? '';
    if (!hash_equals(csrf_token(), $header)) {
        json_response(['error' => 'CSRF invalido'], 403);
    }
}

function password_is_valid(string $password): bool
{
    $hash = getenv('PLAYER_PASSWORD_HASH') ?: '';
    if ($hash !== '') {
        return password_verify($password, $hash);
    }

    $plain = getenv('PLAYER_PASSWORD') ?: 'player-local';
    return hash_equals($plain, $password);
}

function numeric_or_null(mixed $value): ?float
{
    if ($value === null || $value === '') {
        return null;
    }
    return is_numeric($value) ? (float) $value : null;
}

function parse_csv_points(string $path): array
{
    $handle = fopen($path, 'rb');
    if ($handle === false) {
        throw new RuntimeException('No se pudo leer el CSV');
    }

    $headers = fgetcsv($handle, 0, ',', '"', '\\');
    if (!is_array($headers)) {
        throw new RuntimeException('CSV vacio');
    }
    $headers = array_map(static fn ($h) => trim((string) $h), $headers);
    $index = array_flip($headers);
    foreach (['captured_at_iso', 'gps_lat', 'gps_lon'] as $required) {
        if (!array_key_exists($required, $index)) {
            throw new RuntimeException("Falta columna requerida: {$required}");
        }
    }

    $points = [];
    $seq = 0;
    while (($row = fgetcsv($handle, 0, ',', '"', '\\')) !== false) {
        if (count(array_filter($row, static fn ($v) => trim((string) $v) !== '')) === 0) {
            continue;
        }
        $item = [];
        foreach ($headers as $i => $header) {
            $item[$header] = isset($row[$i]) ? trim((string) $row[$i], "\x00 \t\n\r\0\x0B") : '';
        }

        $lat = numeric_or_null($item['gps_lat'] ?? null);
        $lon = numeric_or_null($item['gps_lon'] ?? null);
        $date = $item['captured_at_iso'] ?? '';
        if ($lat === null || $lon === null || $date === '' || strtotime($date) === false) {
            continue;
        }

        $measures = $item;
        unset($measures['location'], $measures['gps_lat'], $measures['gps_lon'], $measures['gps_alt'], $measures['captured_at_iso'], $measures['captured_at_epoch']);

        $points[] = [
            'seq' => $seq++,
            'captured_at_iso' => $date,
            'captured_at_epoch' => numeric_or_null($item['captured_at_epoch'] ?? null),
            'gps_lat' => $lat,
            'gps_lon' => $lon,
            'gps_alt' => numeric_or_null($item['gps_alt'] ?? null),
            'measures_json' => json_encode($measures, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES),
        ];
    }
    fclose($handle);

    if (!$points) {
        throw new RuntimeException('No hay puntos GPS validos');
    }
    return $points;
}

function route_row_to_api(array $row): array
{
    return [
        'id' => (int) $row['id'],
        'title' => $row['title'],
        'sourceFilename' => $row['source_filename'],
        'isPublic' => (bool) $row['is_public'],
        'createdAt' => $row['created_at'],
        'pointCount' => (int) $row['point_count'],
        'startedAt' => $row['started_at'],
        'endedAt' => $row['ended_at'],
    ];
}

function api_routes(): void
{
    $pdo = db();
    $private = is_private();
    $sql = 'SELECT * FROM routes ' . ($private ? '' : 'WHERE is_public = 1 ') . 'ORDER BY created_at DESC, id DESC';
    $routes = array_map('route_row_to_api', $pdo->query($sql)->fetchAll());
    json_response(['routes' => $routes, 'private' => $private, 'csrf' => csrf_token()]);
}

function api_route(int $id): void
{
    $pdo = db();
    $private = is_private();
    $routeStmt = $pdo->prepare('SELECT * FROM routes WHERE id = :id' . ($private ? '' : ' AND is_public = 1'));
    $routeStmt->execute([':id' => $id]);
    $route = $routeStmt->fetch();
    if (!$route) {
        json_response(['error' => 'Ruta no encontrada'], 404);
    }

    $pointStmt = $pdo->prepare('SELECT * FROM points WHERE route_id = :id ORDER BY seq ASC');
    $pointStmt->execute([':id' => $id]);
    $points = [];
    foreach ($pointStmt->fetchAll() as $point) {
        $points[] = [
            'seq' => (int) $point['seq'],
            'captured_at_iso' => $point['captured_at_iso'],
            'captured_at_epoch' => numeric_or_null($point['captured_at_epoch']),
            'gps_lat' => (float) $point['gps_lat'],
            'gps_lon' => (float) $point['gps_lon'],
            'gps_alt' => numeric_or_null($point['gps_alt']),
            'measures' => json_decode($point['measures_json'], true, flags: JSON_THROW_ON_ERROR),
        ];
    }
    json_response(['route' => route_row_to_api($route), 'points' => $points]);
}

function api_login(): void
{
    if (request_method() !== 'POST') {
        json_response(['error' => 'Metodo no permitido'], 405);
    }

    $payload = json_decode(file_get_contents('php://input') ?: '{}', true);
    $password = is_array($payload) ? (string) ($payload['password'] ?? '') : '';
    if (!password_is_valid($password)) {
        usleep(250000);
        json_response(['error' => 'Password incorrecta'], 403);
    }

    session_regenerate_id(true);
    $_SESSION['player_private'] = true;
    csrf_token();
    json_response(['ok' => true, 'csrf' => csrf_token()]);
}

function api_logout(): void
{
    if (request_method() !== 'POST') {
        json_response(['error' => 'Metodo no permitido'], 405);
    }
    require_csrf();
    $_SESSION = [];
    session_destroy();
    json_response(['ok' => true]);
}

function api_upload(): void
{
    if (request_method() !== 'POST') {
        json_response(['error' => 'Metodo no permitido'], 405);
    }
    require_private();
    require_csrf();

    if (empty($_FILES['csv']) || !is_uploaded_file($_FILES['csv']['tmp_name'])) {
        json_response(['error' => 'CSV requerido'], 400);
    }
    if ((int) $_FILES['csv']['size'] > MAX_UPLOAD_BYTES) {
        json_response(['error' => 'CSV demasiado grande'], 400);
    }

    $name = basename((string) $_FILES['csv']['name']);
    if (!preg_match('/\\.csv$/i', $name)) {
        json_response(['error' => 'Solo se aceptan CSV'], 400);
    }

    try {
        $points = parse_csv_points($_FILES['csv']['tmp_name']);
    } catch (Throwable $error) {
        json_response(['error' => $error->getMessage()], 400);
    }

    $title = trim((string) ($_POST['title'] ?? ''));
    if ($title === '') {
        $title = preg_replace('/\\.csv$/i', '', $name) ?: 'Ruta';
    }
    $title = mb_substr($title, 0, 120);
    $isPublic = !empty($_POST['is_public']) ? 1 : 0;

    $pdo = db();
    $pdo->beginTransaction();
    try {
        $routeStmt = $pdo->prepare(
            'INSERT INTO routes (title, source_filename, is_public, point_count, started_at, ended_at)
             VALUES (:title, :source, :public, :count, :started, :ended)'
        );
        $routeStmt->execute([
            ':title' => $title,
            ':source' => $name,
            ':public' => $isPublic,
            ':count' => count($points),
            ':started' => $points[0]['captured_at_iso'],
            ':ended' => $points[count($points) - 1]['captured_at_iso'],
        ]);
        $routeId = (int) $pdo->lastInsertId();

        $pointStmt = $pdo->prepare(
            'INSERT INTO points
             (route_id, seq, captured_at_iso, captured_at_epoch, gps_lat, gps_lon, gps_alt, measures_json)
             VALUES (:route_id, :seq, :captured_at_iso, :captured_at_epoch, :gps_lat, :gps_lon, :gps_alt, :measures_json)'
        );
        foreach ($points as $point) {
            $pointStmt->execute([
                ':route_id' => $routeId,
                ':seq' => $point['seq'],
                ':captured_at_iso' => $point['captured_at_iso'],
                ':captured_at_epoch' => $point['captured_at_epoch'],
                ':gps_lat' => $point['gps_lat'],
                ':gps_lon' => $point['gps_lon'],
                ':gps_alt' => $point['gps_alt'],
                ':measures_json' => $point['measures_json'],
            ]);
        }
        $pdo->commit();
    } catch (Throwable $error) {
        $pdo->rollBack();
        json_response(['error' => 'No se pudo guardar la ruta'], 500);
    }

    json_response(['ok' => true, 'routeId' => $routeId]);
}

function api_rename_route(): void
{
    if (request_method() !== 'POST') {
        json_response(['error' => 'Metodo no permitido'], 405);
    }
    require_private();
    require_csrf();

    $payload = json_decode(file_get_contents('php://input') ?: '{}', true);
    $id = is_array($payload) ? (int) ($payload['id'] ?? 0) : 0;
    $title = is_array($payload) ? trim((string) ($payload['title'] ?? '')) : '';
    if ($id <= 0 || $title === '') {
        json_response(['error' => 'Titulo requerido'], 400);
    }
    $title = mb_substr($title, 0, 120);

    $stmt = db()->prepare('UPDATE routes SET title = :title WHERE id = :id');
    $stmt->execute([':title' => $title, ':id' => $id]);
    if ($stmt->rowCount() === 0) {
        json_response(['error' => 'Ruta no encontrada'], 404);
    }
    json_response(['ok' => true]);
}

function api_delete_route(): void
{
    if (request_method() !== 'POST') {
        json_response(['error' => 'Metodo no permitido'], 405);
    }
    require_private();
    require_csrf();

    $payload = json_decode(file_get_contents('php://input') ?: '{}', true);
    $id = is_array($payload) ? (int) ($payload['id'] ?? 0) : 0;
    if ($id <= 0) {
        json_response(['error' => 'Ruta requerida'], 400);
    }

    $stmt = db()->prepare('DELETE FROM routes WHERE id = :id');
    $stmt->execute([':id' => $id]);
    if ($stmt->rowCount() === 0) {
        json_response(['error' => 'Ruta no encontrada'], 404);
    }
    json_response(['ok' => true]);
}

$api = $_GET['api'] ?? null;
if ($api !== null) {
    try {
        match ($api) {
            'routes' => api_routes(),
            'route' => api_route((int) ($_GET['id'] ?? 0)),
            'login' => api_login(),
            'logout' => api_logout(),
            'upload' => api_upload(),
            'renameRoute' => api_rename_route(),
            'deleteRoute' => api_delete_route(),
            default => json_response(['error' => 'API no encontrada'], 404),
        };
    } catch (Throwable $error) {
        json_response(['error' => 'Error interno'], 500);
    }
}
?>
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?= htmlspecialchars(APP_NAME, ENT_QUOTES) ?></title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
  <link rel="stylesheet" href="assets/app.css">
</head>
<body>
  <main class="app">
    <aside class="sidebar">
      <header>
        <div class="brand">
          <img src="assets/rotor-logo.png" alt="Rotor">
          <div>
            <h1>NUBEMOVIL</h1>
            <a class="studio-link" href="https://www.rotor-studio.net/v3/2026/nube-movil-es/" target="_blank" rel="noopener">by ROTOR STUDIO</a>
          </div>
        </div>
      </header>

      <section class="panel">
        <p class="panel-title">Publico</p>
        <label for="routeSelect">Ruta</label>
        <select id="routeSelect"></select>
        <label for="metricSelect">Sensor</label>
        <select id="metricSelect"></select>
        <button id="emojiToggle" class="secondary" type="button">Emoji temperatura</button>
      </section>

      <section class="panel">
        <p class="panel-title">Play</p>
        <div class="controls">
          <button id="playButton" type="button">></button>
          <input id="timeline" type="range" min="0" value="0" step="1">
          <select id="playSpeedSelect" aria-label="Velocidad de reproduccion">
            <option value="1">x1</option>
            <option value="3">x3</option>
            <option value="5">x5</option>
            <option value="10">x10</option>
          </select>
          <span id="timeLabel">--:--</span>
        </div>
      </section>

      <section class="panel">
        <p class="panel-title">Datos</p>
        <div id="dataPanel" class="data-panel"></div>
      </section>

    </aside>
    <div id="map"></div>
    <section class="private-panel">
      <div class="private-header">
        <p class="panel-title">Privado</p>
        <div id="privateStatus" class="status"></div>
      </div>
      <form id="loginForm" class="login-row">
        <label for="passwordInput">Password</label>
        <input id="passwordInput" type="password" autocomplete="current-password">
        <button type="submit">Entrar</button>
      </form>

      <div id="privateTools" class="private-tools hidden">
        <form id="uploadForm" class="tool-block upload-tool">
          <p class="panel-title">Subir ruta</p>
          <label for="routeTitleInput">Titulo</label>
          <input id="routeTitleInput" type="text" maxlength="120" placeholder="Nombre de la ruta">
          <label for="csvInput">CSV</label>
          <input id="csvInput" class="file-native" type="file" accept=".csv,text/csv">
          <button id="pickCsvButton" class="file-picker" type="button">
            <span>Seleccionar CSV</span>
            <strong id="csvFileName">Ningun archivo seleccionado</strong>
          </button>
          <label class="check"><input id="publicInput" type="checkbox" checked> Publicar ruta</label>
          <button type="submit">Subir ruta</button>
        </form>

        <div class="tool-block manage-tool">
          <p class="panel-title">Gestion rutas</p>
          <label for="routeRenameInput">Nombre ruta actual</label>
          <input id="routeRenameInput" type="text" maxlength="120">
          <div class="button-row">
            <button id="renameRouteButton" type="button">Renombrar</button>
            <button id="deleteRouteButton" class="danger" type="button">Borrar</button>
          </div>
        </div>

        <div class="tool-block export-tool">
          <p class="panel-title">Exportar</p>
          <label for="exportFormatSelect">Formato</label>
          <select id="exportFormatSelect">
            <option value="landscape">Horizontal 1920x1080</option>
            <option value="portrait">Vertical 1080x1920</option>
            <option value="square">Cuadrado 1600x1600</option>
          </select>
          <label for="exportMapSelect">Mapa exportacion</label>
          <select id="exportMapSelect">
            <option value="carto_light" selected>CARTO claro</option>
            <option value="osm">OpenStreetMap</option>
          </select>
          <label for="exportZoomSelect">Zoom exportacion</label>
          <select id="exportZoomSelect">
            <option value="far">Lejos</option>
            <option value="normal" selected>Normal</option>
            <option value="near">Cerca</option>
            <option value="detail">Detalle</option>
          </select>
          <label for="exportScaleSelect">Calidad</label>
          <select id="exportScaleSelect">
            <option value="1">Normal</option>
            <option value="2" selected>Alta x2</option>
          </select>
          <div class="button-row">
            <button id="exportPngButton" type="button">Exportar PNG actual</button>
            <button id="recordPlayButton" type="button">Grabar PNGs</button>
          </div>
        </div>
        <button id="logoutButton" type="button">Salir</button>
      </div>
    </section>
  </main>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <script src="assets/app.js"></script>
</body>
</html>
