<?php

declare(strict_types=1);

require __DIR__ . '/../../src/bootstrap.php';

ensure_storage();
require_ingest_token();

$payload = ingest_payload();
$jsonBody = request_json_body();

$currentState = read_state();

$nextState = [
    'title' => (string) ($payload['title'] ?? $currentState['title'] ?? app_config()['site_name']),
    'subtitle' => (string) ($payload['subtitle'] ?? $currentState['subtitle'] ?? 'Esperando datos del Aula Móvil'),
    'location' => (string) ($payload['location'] ?? $currentState['location'] ?? ''),
    'updated_at' => gmdate('c'),
    'metrics' => normalize_metrics($payload['metrics'] ?? []),
    'camera' => [
        'has_frame' => is_file(latest_frame_path()),
    ],
];

if (isset($_FILES['frame']) && is_uploaded_file($_FILES['frame']['tmp_name'])) {
    $target = latest_frame_path();
    move_uploaded_file($_FILES['frame']['tmp_name'], $target);

    $archiveName = gmdate('Ymd_His') . '.jpg';
    copy($target, storage_path('frames/' . $archiveName));
    $nextState['camera']['has_frame'] = true;
    $nextState['camera']['archive_name'] = $archiveName;
} else {
    $archiveName = persist_frame_from_base64($_POST['frame_base64'] ?? $jsonBody['frame_base64'] ?? null);
    if (is_string($archiveName)) {
        $nextState['camera']['has_frame'] = true;
        $nextState['camera']['archive_name'] = $archiveName;
    }
}

write_state($nextState);

json_response([
    'ok' => true,
    'saved' => $nextState,
]);
