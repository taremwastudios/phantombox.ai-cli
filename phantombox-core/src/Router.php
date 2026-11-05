<?php
/**
 * PhantomBox Core - HTTP Router / Front controller dispatch
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core;

use PhantomBox\Core\Controllers\AuthorizationController;
use PhantomBox\Core\Controllers\IdentityController;
use PhantomBox\Core\Controllers\IdentityPageController;
use PhantomBox\Core\Services\ClientType;

class Router
{
    public function dispatch(): void
    {
        header('X-Content-Type-Options: nosniff');
        header('Cache-Control: no-store');

        $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
        $uri = $_SERVER['REQUEST_URI'] ?? '/';
        $path = rtrim(parse_url($uri, PHP_URL_PATH) ?: '/', '/');

        // Controllers are created LAZILY, inside the route that needs them.
        // Constructing AuthorizationController opens a MySQL connection, so
        // eager construction made every request (including plain HTML pages)
        // depend on the database and returned HTTP 500 when it was unavailable.
        $authz = null;
        $identity = null;

        try {
            // ---------------- Identity lifecycle (existing contract) -------
            if ($method === 'POST' && $path === '/v1/identity/email/request') {
                ($identity ??= new IdentityController())->requestOtp();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/register') {
                ($identity ??= new IdentityController())->register();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/email/verify') {
                ($identity ??= new IdentityController())->verifyOtp();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/profile/complete') {
                ($identity ??= new IdentityController())->completeProfile();
                return;
            }
            if ($method === 'GET' && $path === '/v1/identity/profile') {
                ($identity ??= new IdentityController())->profile();
                return;
            }
            if (($method === 'POST' || $method === 'GET') && $path === '/v1/identity/logout') {
                ($identity ??= new IdentityController())->logout();
                return;
            }

            // ---------------- Shared authorization lifecycle ---------------
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization') {
                ($authz ??= new AuthorizationController())->create();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/poll') {
                ($authz ??= new AuthorizationController())->poll();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/redeem') {
                ($authz ??= new AuthorizationController())->redeem();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/approve') {
                ($authz ??= new AuthorizationController())->approve();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/deny') {
                ($authz ??= new AuthorizationController())->deny();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/expire') {
                ($authz ??= new AuthorizationController())->expire();
                return;
            }
            if ($method === 'GET' && preg_match('#^/v1/identity/cli/authorization/([^/]+)$#', $path, $m)) {
                ($authz ??= new AuthorizationController())->show($m[1]);
                return;
            }

            // ---------------- Identity HTML pages (browser UI) ------------
            // Presentation only: every authentication call still goes to the
            // JSON API routes above. The page controller is instantiated here
            // and only here, so HTML rendering never touches the database.
            if ($method === 'GET' && $path === '/sign-in/cli/identity') {
                (new IdentityPageController())->deviceAuthorization();
                return;
            }

            // Direct access to the authentication interface is not allowed:
            // the UI is only served after Core validates a CLI identity link.
            if ($method === 'GET' && ($path === '/sign-in' || $path === '/sign-up')) {
                (new IdentityPageController())->rejectDirectAccess();
                return;
            }

            // Health / contract probe
            if ($method === 'GET' && $path === '/health') {
                self::json(['ok' => true, 'service' => 'phantombox-core']);
                return;
            }

            self::json(['ok' => false, 'error' => 'Endpoint not found.'], 404);
        } catch (\Throwable $e) {
            error_log('PhantomBox Core error: ' . $e->getMessage());
            self::json(['ok' => false, 'error' => 'Internal server error.'], 500);
        }
    }

    private static function json(array $data, int $statusCode = 200): void
    {
        http_response_code($statusCode);
        header('Content-Type: application/json; charset=UTF-8');
        echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        exit;
    }
}
