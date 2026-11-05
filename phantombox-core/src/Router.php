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

        $authz = new AuthorizationController();
        $identity = new IdentityController();

        try {
            // ---------------- Identity lifecycle (existing contract) -------
            if ($method === 'POST' && $path === '/v1/identity/email/request') {
                $identity->requestOtp();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/email/verify') {
                $identity->verifyOtp();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/profile/complete') {
                $identity->completeProfile();
                return;
            }
            if ($method === 'GET' && $path === '/v1/identity/profile') {
                $identity->profile();
                return;
            }
            if (($method === 'POST' || $method === 'GET') && $path === '/v1/identity/logout') {
                $identity->logout();
                return;
            }

            // ---------------- Shared authorization lifecycle ---------------
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization') {
                $authz->create();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/poll') {
                $authz->poll();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/redeem') {
                $authz->redeem();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/approve') {
                $authz->approve();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/deny') {
                $authz->deny();
                return;
            }
            if ($method === 'POST' && $path === '/v1/identity/cli/authorization/expire') {
                $authz->expire();
                return;
            }
            if ($method === 'GET' && preg_match('#^/v1/identity/cli/authorization/([^/]+)$#', $path, $m)) {
                $authz->show($m[1]);
                return;
            }

            // ---------------- Identity HTML pages (browser UI) ------------
            // Presentation only: every authentication call still goes to the
            // JSON API routes above.
            if ($method === 'GET' && ($path === '/sign-in' || $path === '/sign-up')) {
                (new IdentityPageController())->signIn();
                return;
            }
            if ($method === 'GET' && $path === '/sign-in/cli/identity') {
                (new IdentityPageController())->deviceAuthorization();
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
