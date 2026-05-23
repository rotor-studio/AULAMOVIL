<?php

declare(strict_types=1);

$config = require __DIR__ . '/../config/config.php';

function app_config(): array
{
    global $config;
    return $config;
}

function storage_path(string $relative = ''): string
{
    $base = rtrim(app_config()['storage_dir'], '/\\');
    return $relative === '' ? $base : $base . DIRECTORY_SEPARATOR . ltrim($relative, '/\\');
}

function ensure_storage(): void
{
    $directories = [
        storage_path(),
        storage_path('frames'),
    ];

    foreach ($directories as $directory) {
        if (!is_dir($directory)) {
            mkdir($directory, 0775, true);
        }
    }
}

function latest_state_path(): string
{
    return storage_path('latest_state.json');
}

function latest_frame_path(): string
{
    return storage_path('latest_frame.jpg');
}

function read_state(): array
{
    $path = latest_state_path();
    if (!is_file($path)) {
        return [
            'title' => 'MOVIL CLOUD',
            'subtitle' => 'Esperando datos del Aula Móvil',
            'updated_at' => null,
            'location' => null,
            'metrics' => [],
            'camera' => [
                'has_frame' => is_file(latest_frame_path()),
            ],
        ];
    }

    $json = file_get_contents($path);
    $data = json_decode($json ?: '{}', true);

    return is_array($data) ? $data : [];
}

function state_updated_at(?array $state = null): ?DateTimeImmutable
{
    $updatedAt = (string) (($state ?? [])['updated_at'] ?? '');
    if ($updatedAt === '') {
        return null;
    }

    try {
        return new DateTimeImmutable($updatedAt);
    } catch (Exception) {
        return null;
    }
}

function state_age_seconds(?array $state = null): ?int
{
    $updatedAt = state_updated_at($state);
    if (!$updatedAt) {
        return null;
    }

    $age = time() - $updatedAt->getTimestamp();
    return max(0, (int) $age);
}

function state_is_online(?array $state = null): bool
{
    $age = state_age_seconds($state);
    if ($age === null) {
        return false;
    }

    $thresholdSec = max(3, (int) ceil(((int) app_config()['state_refresh_ms'] * 3) / 1000));
    return $age <= $thresholdSec;
}

function write_state(array $state): void
{
    ensure_storage();
    file_put_contents(
        latest_state_path(),
        json_encode($state, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)
    );
}

function json_response(array $payload, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function request_token(): string
{
    $jsonBody = request_json_body();
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (preg_match('/Bearer\s+(.+)/i', $header, $matches)) {
        return trim($matches[1]);
    }

    return trim((string) ($_POST['token'] ?? $_GET['token'] ?? $_SERVER['HTTP_X_INGEST_TOKEN'] ?? $jsonBody['token'] ?? ''));
}

function require_ingest_token(): void
{
    if (request_token() !== (string) app_config()['ingest_token']) {
        json_response(['ok' => false, 'error' => 'Unauthorized'], 401);
    }
}

function normalize_metrics(array $metrics): array
{
    $normalized = [];

    foreach ($metrics as $metric) {
        if (!is_array($metric)) {
            continue;
        }

        $normalized[] = [
            'label' => (string) ($metric['label'] ?? 'Dato'),
            'value' => (string) ($metric['value'] ?? '--'),
            'unit' => (string) ($metric['unit'] ?? ''),
            'device' => (string) ($metric['device'] ?? ''),
            'metric_id' => (string) ($metric['metric_id'] ?? ''),
        ];
    }

    return $normalized;
}

function request_json_body(): array
{
    static $payload;

    if (is_array($payload)) {
        return $payload;
    }

    $raw = file_get_contents('php://input');
    if (!is_string($raw) || trim($raw) === '') {
        $payload = [];
        return $payload;
    }

    $decoded = json_decode($raw, true);
    $payload = is_array($decoded) ? $decoded : [];
    return $payload;
}

function ingest_payload(): array
{
    $jsonBody = request_json_body();
    $payloadRaw = (string) ($_POST['payload'] ?? '');
    $payload = json_decode($payloadRaw, true);

    if (is_array($payload)) {
        return $payload;
    }

    if (isset($jsonBody['payload']) && is_array($jsonBody['payload'])) {
        return $jsonBody['payload'];
    }

    $directMetrics = $_POST['metrics'] ?? $jsonBody['metrics'] ?? [];
    if (is_string($directMetrics)) {
        $decodedMetrics = json_decode($directMetrics, true);
        $directMetrics = is_array($decodedMetrics) ? $decodedMetrics : [];
    }

    return [
        'title' => $_POST['title'] ?? $jsonBody['title'] ?? null,
        'subtitle' => $_POST['subtitle'] ?? $jsonBody['subtitle'] ?? null,
        'location' => $_POST['location'] ?? $jsonBody['location'] ?? null,
        'metrics' => is_array($directMetrics) ? $directMetrics : [],
        'display' => is_array($_POST['display'] ?? $jsonBody['display'] ?? null) ? ($_POST['display'] ?? $jsonBody['display']) : null,
        'headline' => $_POST['headline'] ?? $jsonBody['headline'] ?? null,
        'line1' => $_POST['line1'] ?? $jsonBody['line1'] ?? null,
        'line2' => $_POST['line2'] ?? $jsonBody['line2'] ?? null,
    ];
}

function persist_frame_from_base64(?string $encodedFrame): ?string
{
    if (!is_string($encodedFrame) || trim($encodedFrame) === '') {
        return null;
    }

    $clean = preg_replace('#^data:image/\\w+;base64,#i', '', trim($encodedFrame));
    $binary = base64_decode($clean ?: '', true);

    if ($binary === false || $binary === '') {
        return null;
    }

    $target = latest_frame_path();
    file_put_contents($target, $binary);

    $archiveName = gmdate('Ymd_His') . '.jpg';
    copy($target, storage_path('frames/' . $archiveName));

    return $archiveName;
}
