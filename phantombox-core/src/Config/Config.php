<?php
/**
 * PhantomBox Core Configuration
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Config;

class Config
{
    private static ?array $settings = null;

    public static function load(): void
    {
        if (self::$settings !== null) {
            return;
        }

        // Load .env file if it exists in the project root
        $envPath = dirname(__DIR__, 2) . '/.env';
        if (file_exists($envPath)) {
            $lines = file($envPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
            foreach ($lines as $line) {
                $line = trim($line);
                if ($line === '' || str_starts_with($line, '#')) {
                    continue;
                }
                if (str_contains($line, '=')) {
                    [$key, $value] = explode('=', $line, 2);
                    $key = trim($key);
                    $value = trim($value, " \t\n\r\0\x0B\"'");
                    if (!array_key_exists($key, $_SERVER) && !array_key_exists($key, $_ENV)) {
                        putenv("{$key}={$value}");
                        $_ENV[$key] = $value;
                        $_SERVER[$key] = $value;
                    }
                }
            }
        }

        self::$settings = [
            'app_name' => self::env('APP_NAME', 'PhantomBox Core'),
            'company_name' => self::env('COMPANY_NAME', 'Phantom Illusions Studio'),
            'app_url' => rtrim(self::env('APP_URL', 'https://core-phantombox.phantom-illusions-studio.com'), '/'),
            'identity_app_url' => rtrim(self::env('IDENTITY_APP_URL', 'https://phantomboxai.phantom-illusions-studio.com'), '/'),
            'app_debug' => filter_var(self::env('APP_DEBUG', 'false'), FILTER_VALIDATE_BOOLEAN),

            // Database (Core MariaDB)
            'db_host' => self::env('DB_HOST', '127.0.0.1'),
            'db_port' => self::env('DB_PORT', '3306'),
            'db_name' => self::env('DB_NAME', 'phantombox_core'),
            'db_user' => self::env('DB_USER', 'phantombox'),
            'db_pass' => self::env('DB_PASS', ''),

            // Authorization transaction policy
            'authz_ttl' => (int) self::env('AUTHZ_TTL', '600'),            // 10 minutes
            'authz_poll_interval' => (int) self::env('AUTHZ_POLL_INTERVAL', '5'),
            'authz_credential_ttl' => (int) self::env('AUTHZ_CREDENTIAL_TTL', '60'), // seconds to retrieve issued credentials
        ];
    }

    public static function get(string $key, mixed $default = null): mixed
    {
        self::load();
        return self::$settings[$key] ?? $default;
    }

    public static function env(string $key, ?string $default = null): ?string
    {
        $val = getenv($key);
        if ($val !== false && $val !== '') {
            return $val;
        }
        if (isset($_ENV[$key]) && $_ENV[$key] !== '') {
            return (string) $_ENV[$key];
        }
        if (isset($_SERVER[$key]) && $_SERVER[$key] !== '') {
            return (string) $_SERVER[$key];
        }
        return $default;
    }
}
