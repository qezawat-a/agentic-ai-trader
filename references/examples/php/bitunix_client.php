<?php
// Bitunix Futures — PHP reference client (no third-party deps).
// Run: php bitunix_client.php

const BITUNIX_BASE = "https://fapi.bitunix.com";

/**
 * Sorted key+value concatenation, no '=' and no '&', empty skipped.
 */
function bitunix_sign_query_string(?array $params): string {
    if (!$params) return "";
    ksort($params);
    $parts = [];
    foreach ($params as $k => $v) {
        if ($v === null || $v === "") continue;
        $parts[] = $k . $v;
    }
    return implode("", $parts);
}

/**
 * sign = sha256(sha256(nonce + timestamp + apiKey + query + body) + secret).
 */
function bitunix_sign(string $apiKey, string $secret, string $nonce,
                      string $timestamp, string $query = "", string $body = ""): string {
    $message = $nonce . $timestamp . $apiKey . $query . $body;
    $digest = hash("sha256", $message);
    return hash("sha256", $digest . $secret);
}

/**
 * Build signed headers for an authenticated request.
 */
function bitunix_headers(string $apiKey, string $secret,
                         ?array $params = null, ?array $body = null): array {
    $nonce = sprintf("%s-%s", time(), bin2hex(random_bytes(8)));
    $timestamp = (string)(int)(microtime(true) * 1000); // milliseconds
    $qs = bitunix_sign_query_string($params);
    $payload = $body === null ? "" : json_encode($body, JSON_UNESCAPED_SLASHES);
    return [
        "api-key" => $apiKey,
        "nonce" => $nonce,
        "timestamp" => $timestamp,
        "sign" => bitunix_sign($apiKey, $secret, $nonce, $timestamp, $qs, $payload),
        "language" => "en-US",
        "Content-Type" => "application/json",
    ];
}

// --- self-check against the official test vector ----------------------------
$apiKey = "11111111111111111";
$secret = "22222222222222222";
$nonce = "33333333333333333";
$timestamp = "1747104711262";
$want = "40e87256554e263e39201a3979469b7fbb640d1c7c41759abf5c6a18f937f922";

$got = bitunix_sign($apiKey, $secret, $nonce, $timestamp, "marginCoinUSDT");
if ($got !== $want) { echo "FAIL sign vector: $got\n"; exit(1); }
$wrong = bitunix_sign($apiKey, $secret, $nonce, $timestamp, "marginCoin=USDT");
if ($wrong === $want) { echo "FAIL urlencoded-guard\n"; exit(1); }
echo "PASS sign vector / urlencoded-guard\n";