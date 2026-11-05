<?php
/**
 * PhantomBox Core - PSR-4 style autoloader
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

spl_autoload_register(function (string $class): void {
    $prefix = 'PhantomBox\\Core\\';
    if (!str_starts_with($class, $prefix)) {
        return;
    }
    $relative = substr($class, strlen($prefix));
    $file = __DIR__ . '/' . str_replace('\\', '/', $relative) . '.php';
    if (is_file($file)) {
        require $file;
    }
});
