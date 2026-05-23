<?php

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$state = read_state();
$framePath = latest_frame_path();
$state['camera'] = $state['camera'] ?? [];
$state['camera']['has_frame'] = is_file($framePath);
$state['camera']['frame_url'] = 'cloud_frame.php?t=' . (is_file($framePath) ? filemtime($framePath) : time());
$state['updated_age_seconds'] = state_age_seconds($state);
$state['is_online'] = state_is_online($state);

json_response($state);
