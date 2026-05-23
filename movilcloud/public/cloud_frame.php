<?php

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$frame = latest_frame_path();

if (!is_file($frame)) {
    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    echo 'No frame received yet.';
    exit;
}

header('Content-Type: image/jpeg');
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
readfile($frame);
exit;
