<?php
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
    http_response_code(204);
    exit;
}

$raw = file_get_contents('php://input');
$body = json_decode($raw ?: '', true);
$source = is_array($body) ? ($body['awb'] ?? '') : ($_GET['awb'] ?? '');
$awb = strtoupper(preg_replace('/[^A-Za-z0-9]/', '', (string) $source));
if (strlen($awb) < 8 || strlen($awb) > 40) {
    echo json_encode([
        'ok' => false,
        'code' => 'invalid',
        'message' => 'Enter a valid AWB or tracking number.',
    ]);
    exit;
}

$delhivery = track_delhivery($awb);
if ($delhivery['ok'] ?? false) {
    echo json_encode($delhivery);
    exit;
}
$courier = track_courier_wala($awb);
if ($courier['ok'] ?? false) {
    echo json_encode($courier);
    exit;
}
if (($delhivery['code'] ?? '') === 'unavailable' && ($courier['code'] ?? '') === 'unavailable') {
    echo json_encode([
        'ok' => false,
        'code' => 'unavailable',
        'message' => 'The courier tracking service could not be reached. Please try again in a moment.',
    ]);
    exit;
}
echo json_encode([
    'ok' => false,
    'code' => 'not_found',
    'message' => 'No shipment was found for this tracking number.',
]);

function track_delhivery($awb)
{
    $url = 'https://dlv-api.delhivery.com/v3/unified-tracking-new?wbn=' . rawurlencode($awb);
    $result = http_json('GET', $url, [
        'Accept: application/json',
        'Origin: https://www.delhivery.com',
        'Referer: https://www.delhivery.com/',
        'User-Agent: Mozilla/5.0',
    ]);
    if ($result === null) {
        return ['ok' => false, 'code' => 'unavailable'];
    }
    $rows = $result['data'] ?? null;
    if (!is_array($rows) || !$rows || !is_array($rows[0])) {
        return ['ok' => false, 'code' => 'not_found'];
    }
    $item = $rows[0];
    $current = is_array($item['status'] ?? null) ? $item['status'] : [];
    $headline = pretty_status($current['status'] ?? ($current['instructions'] ?? ''));
    $events = [];
    foreach ($item['trackingStates'] ?? [] as $state) {
        if (!is_array($state)) {
            continue;
        }
        foreach ($state['scans'] ?? [] as $scan) {
            if (!is_array($scan)) {
                continue;
            }
            $description = trim((string) ($scan['scanNslRemark'] ?? ($scan['scan'] ?? ($current['instructions'] ?? ''))));
            if ($description === '') {
                continue;
            }
            $events[] = [
                'time' => format_time($scan['scanDateTime'] ?? ($scan['scanDate'] ?? ($current['statusDateTime'] ?? ''))),
                'description' => $description,
                'location' => trim((string) ($scan['scannedLocation'] ?? ($scan['cityLocation'] ?? ''))),
            ];
        }
    }
    if (!$events && $headline !== '') {
        $events[] = [
            'time' => format_time($current['statusDateTime'] ?? ''),
            'description' => trim((string) ($current['instructions'] ?? $headline)),
            'location' => '',
        ];
    }
    $location = $events[0]['location'] ?? '';
    $tone = tone_of($headline);
    return [
        'ok' => true,
        'shipment' => [
            'carrier' => 'delhivery',
            'carrierName' => 'Delhivery',
            'awb' => trim((string) ($item['awb'] ?? $awb)),
            'status' => $headline !== '' ? $headline : 'Status update',
            'tone' => $tone,
            'location' => $location,
            'eta' => $tone === 'ok' ? '' : format_time($item['promiseDeliveryDate'] ?? ($item['deliveryDateText'] ?? '')),
            'events' => array_slice($events, 0, 12),
        ],
    ];
}

function track_courier_wala($awb)
{
    $result = http_json('POST', 'https://xpresion.courierwalaexpress.in/api/v1/Tracking/Tracking', [
        'Content-Type: application/json',
        'Accept: application/json',
    ], json_encode([
        'UserID' => 'CUSTRC',
        'Password' => '83D54B1C37',
        'AWBNo' => $awb,
        'Type' => 'A',
        'RequiredUrl' => 'yes',
        'ShowAllFields' => 'yes',
    ]));
    if ($result === null) {
        return ['ok' => false, 'code' => 'unavailable'];
    }
    $payload = is_array($result['Response'] ?? null) ? $result['Response'] : [];
    $disc = (string) ($payload['ErrorDisc'] ?? '');
    $rows = $payload['Tracking'] ?? [];
    if (is_array($rows) && array_keys($rows) !== range(0, count($rows) - 1)) {
        $rows = [$rows];
    }
    if (!is_array($rows) || stripos($disc, 'success') === false || !$rows || !is_array($rows[0])) {
        return ['ok' => false, 'code' => 'not_found'];
    }
    $row = $rows[0];
    $events = [];
    foreach ($payload['Events'] ?? [] as $item) {
        if (!is_array($item)) {
            continue;
        }
        $description = trim((string) ($item['Status'] ?? ($item['Remark'] ?? '')));
        if ($description === '') {
            continue;
        }
        $events[] = [
            'time' => trim(format_time($item['EventDate'] ?? '') . ' ' . clock_text($item['EventTime'] ?? '')),
            'description' => $description,
            'location' => trim((string) ($item['Location'] ?? ($item['City'] ?? ''))),
        ];
    }
    $status = trim((string) ($row['Status'] ?? ($events[0]['description'] ?? 'Status update')));
    $vendor = trim((string) ($row['VendorName'] ?? ($row['Vendor'] ?? '')));
    $tone = tone_of($status);
    return [
        'ok' => true,
        'shipment' => [
            'carrier' => 'courierwala',
            'carrierName' => ($vendor !== '' && !ctype_digit($vendor)) ? $vendor : 'Courier Wala',
            'awb' => trim((string) ($row['AWBNo'] ?? $awb)),
            'status' => $status,
            'tone' => $tone,
            'location' => $events[0]['location'] ?? trim((string) ($row['Destination'] ?? '')),
            'eta' => $tone === 'ok' ? '' : format_time($row['ExpectedDeliveryDate'] ?? ''),
            'events' => array_slice($events, 0, 12),
        ],
    ];
}

function http_json($method, $url, $headers, $body = null)
{
    if (!function_exists('curl_init')) {
        return null;
    }
    $ch = curl_init($url);
    $options = [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 20,
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_HTTPHEADER => $headers,
    ];
    if ($body !== null) {
        $options[CURLOPT_POSTFIELDS] = $body;
    }
    curl_setopt_array($ch, $options);
    $raw = curl_exec($ch);
    $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($raw === false || $code >= 400) {
        return null;
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : null;
}

function pretty_status($value)
{
    $text = trim((string) $value);
    if ($text !== '' && $text === strtoupper($text)) {
        return ucwords(strtolower(str_replace('_', ' ', $text)));
    }
    return $text;
}

function format_time($value)
{
    $text = trim((string) $value);
    if (preg_match('/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/', $text, $match)) {
        return $match[3] . '-' . $match[2] . '-' . $match[1] . ' ' . $match[4] . ':' . $match[5];
    }
    if (preg_match('/^(\d{2})[\/-](\d{2})[\/-](\d{4})/', $text, $match)) {
        return $match[1] . '-' . $match[2] . '-' . $match[3];
    }
    return $text;
}

function clock_text($value)
{
    $text = trim((string) $value);
    if (preg_match('/^(\d{2})(\d{2})(?:\d{2})?$/', $text, $match)) {
        return $match[1] . ':' . $match[2];
    }
    return $text;
}

function tone_of($status)
{
    $text = strtolower((string) $status);
    if (preg_match('/deliver|signed/', $text)) {
        return 'ok';
    }
    if (preg_match('/fail|exception|undeliver|return|cancel|refused/', $text)) {
        return 'bad';
    }
    if (preg_match('/transit|out for|pickup|picked|depart|arriv|customs|manifest/', $text)) {
        return 'move';
    }
    return 'wait';
}
