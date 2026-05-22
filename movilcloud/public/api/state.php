<?php

declare(strict_types=1);

require __DIR__ . '/../../src/bootstrap.php';

$state = read_state();
$framePath = latest_frame_path();
$state['camera'] = $state['camera'] ?? [];
$state['camera']['has_frame'] = is_file($framePath);
$state['camera']['frame_url'] = 'api/frame.php?t=' . (is_file($framePath) ? filemtime($framePath) : time());

json_response($state);
