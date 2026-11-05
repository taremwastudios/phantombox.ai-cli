<?php
/**
 * PhantomBox Core - local PHP development router
 * Usage: php -S 127.0.0.1:8600 -t public router.php
 */

declare(strict_types=1);

// public/index.php loads src/autoload.php itself; the autoloader resolves
// every PhantomBox\Core\* service and controller, so no manual requires are
// needed here.

$uri = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
$file = __DIR__ . '/public' . $uri;

if ($uri !== '/' && file_exists($file) && !is_dir($file)) {
    return false; // serve static files directly
}

$_SERVER['SCRIPT_NAME'] = '/index.php';
require __DIR__ . '/public/index.php';
