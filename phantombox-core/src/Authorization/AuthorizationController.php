<?php
/**
 * PhantomBox Core - HTTP controller for the shared authorization lifecycle
 *
 * Serves Web, CLI and App through ONE set of endpoints. The client type is
 * resolved from the established request header X-PhantomBox-Client and is
 * validated/recorded on every transaction.
 *
 * Authentication delegates to the PRODUCTION services (src/Identity/*):
 *   - Approve/Deny accept the standard production bearer token
 *     (64-char hex auth_sessions token, validated via SessionService).
 *   - Redeem returns a production auth_sessions bearer token.
 *
 * Endpoints:
 *   POST /v1/identity/cli/authorization            create           (any client)
 *   GET  /v1/identity/cli/authorization/{ref}      browser details  (web)
 *   POST /v1/identity/cli/authorization/approve    explicit approve (web)
 *   POST /v1/identity/cli/authorization/deny       explicit deny    (web)
 *   POST /v1/identity/cli/authorization/poll       status polling   (cli|app)
 *   POST /v1/identity/cli/authorization/redeem     credential delivery (cli|app)
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

final class AuthorizationController
{
    private AuthorizationService $authz;

    public function __construct(PDO $database)
    {
        $this->authz = new AuthorizationService($database);
    }

    /**
     * POST /v1/identity/cli/authorization
     * Body: { device_name, platform, cli_version|version, [scope] }
     * Header: X-PhantomBox-Client: web|cli|app
     *
     * Creates a PENDING transaction. Does not authenticate or approve anything.
     */
    public function create(): void
    {
        $clientType = $this->requireClientType();
        if ($clientType === null) {
            return; // response already sent
        }

        $input  = $this->requestJson();
        $device = [
            'device_name' => (string) ($input['device_name'] ?? $input['hostname'] ?? ''),
            'platform'    => (string) ($input['platform'] ?? $input['os'] ?? ''),
            'cli_version' => (string) ($input['cli_version'] ?? $input['version'] ?? ''),
        ];

        if ($device['device_name'] === '' && $device['platform'] === '') {
            self::json(['ok' => false, 'error' => 'Device details (device_name or platform) are required.'], 422);
            return;
        }

        $result = $this->authz->create(
            $clientType,
            $device,
            self::clientIp(),
            isset($input['scope']) ? (string) $input['scope'] : null
        );

        // 201 Created: transaction exists, nothing has been approved.
        self::json($result, 201);
    }

    /**
     * GET /v1/identity/cli/authorization/{reference}
     * Returns browser-facing transaction details. NEVER includes the
     * polling credential (plaintext or hash).
     */
    public function show(string $reference): void
    {
        $reference = trim($reference);

        if (!self::isValidReference($reference)) {
            self::json(['ok' => false, 'error' => 'Invalid authorization reference.'], 404);
            return;
        }

        $result = $this->authz->get($reference);

        if (!$result['found']) {
            self::json(['ok' => false, 'error' => 'Authorization reference not found.'], 404);
            return;
        }

        self::json($result['transaction'], $result['expired'] ? 410 : 200);
    }

    /**
     * POST /v1/identity/cli/authorization/approve
     * Body: { reference }
     * Header: Authorization: Bearer <production session> + X-PhantomBox-Client
     */
    public function approve(): void
    {
        $this->decide('approve');
    }

    /**
     * POST /v1/identity/cli/authorization/deny
     * Body: { reference }
     * Header: Authorization: Bearer <production session> + X-PhantomBox-Client
     */
    public function deny(): void
    {
        $this->decide('deny');
    }

    /**
     * POST /v1/identity/cli/authorization/poll
     * Body: { poll_token }
     * Header: X-PhantomBox-Client: cli|app
     */
    public function poll(): void
    {
        $clientType = $this->requireClientType();
        if ($clientType === null) {
            return;
        }

        if ($clientType === 'web') {
            self::json(['ok' => false, 'error' => 'Polling is not available to browser clients.'], 403);
            return;
        }

        $input     = $this->requestJson();
        $pollToken = trim((string) ($input['poll_token'] ?? ''));

        $result = $this->authz->poll($pollToken);
        self::json(
            self::publicResult($result),
            (int) ($result['http_code'] ?? 200)
        );
    }

    /**
     * POST /v1/identity/cli/authorization/redeem
     * Body: { poll_token }
     * Header: X-PhantomBox-Client: cli|app
     *
     * One-time secure credential delivery after approval. Returns a
     * production auth_sessions bearer token.
     */
    public function redeem(): void
    {
        $clientType = $this->requireClientType();
        if ($clientType === null) {
            return;
        }

        if ($clientType === 'web') {
            self::json(['ok' => false, 'error' => 'Credential delivery is not available to browser clients.'], 403);
            return;
        }

        $input     = $this->requestJson();
        $pollToken = trim((string) ($input['poll_token'] ?? ''));

        $result = $this->authz->redeem($pollToken);
        self::json(
            self::publicResult($result),
            (int) ($result['http_code'] ?? 200)
        );
    }

    // -------------------------------------------------------------------

    private function decide(string $action): void
    {
        $clientType = $this->requireClientType();
        if ($clientType === null) {
            return;
        }

        $sessionToken = self::bearerToken();
        if ($sessionToken === null) {
            self::json(['ok' => false, 'error' => 'Authentication required. Please sign in.'], 401);
            return;
        }

        $input     = $this->requestJson();
        $reference = trim((string) ($input['reference'] ?? ''));

        if (!self::isValidReference($reference)) {
            self::json(['ok' => false, 'error' => 'Missing or invalid authorization reference.'], 422);
            return;
        }

        $result = $action === 'approve'
            ? $this->authz->approve($reference, $sessionToken, $clientType)
            : $this->authz->deny($reference, $sessionToken, $clientType);

        if (!($result['ok'] ?? false)) {
            self::json(
                ['ok' => false, 'error' => $result['error'] ?? 'Request failed.'],
                (int) ($result['code'] ?? 500)
            );
            return;
        }

        self::json([
            'ok'      => true,
            'status'  => $result['status'],
            'message' => $result['message'],
        ], 200);
    }

    // -------------------------------------------------------------------

    /**
     * Resolve and validate the client type header. Sends a 400 response
     * and returns null when the header is missing or unsupported.
     */
    private function requireClientType(): ?string
    {
        $raw   = $_SERVER['HTTP_X_PHANTOMBOX_CLIENT'] ?? '';
        $value = strtolower(trim((string) $raw));

        if ($value === '' || !in_array($value, ['web', 'cli', 'app'], true)) {
            self::json(
                ['ok' => false, 'error' => 'Missing or invalid client identification header. Expected X-PhantomBox-Client: web|cli|app.'],
                400
            );
            return null;
        }

        return $value;
    }

    private static function publicResult(array $result): array
    {
        // Strip internal fields (http_code, code) from the response body.
        $public = [];
        foreach ($result as $key => $value) {
            if ($key === 'http_code' || $key === 'code') {
                continue;
            }
            $public[$key] = $value;
        }
        return $public;
    }

    private static function isValidReference(string $reference): bool
    {
        return (bool) preg_match('/^[a-zA-Z0-9_\-\.:]{6,128}$/', $reference);
    }

    private function requestJson(): array
    {
        $raw = file_get_contents('php://input');
        if (is_string($raw) && $raw !== '') {
            $decoded = json_decode($raw, true);
            if (is_array($decoded)) {
                return $decoded;
            }
        }
        return $_POST;
    }

    private static function bearerToken(): ?string
    {
        $header = $_SERVER['HTTP_AUTHORIZATION']
            ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION']
            ?? '';

        if ($header === '' && function_exists('apache_request_headers')) {
            $headers = array_change_key_case(apache_request_headers(), CASE_LOWER);
            $header  = (string) ($headers['authorization'] ?? '');
        }

        if (preg_match('/^\s*Bearer\s+(.+)$/i', $header, $m)) {
            return trim($m[1]);
        }
        return null;
    }

    private static function clientIp(): string
    {
        $ip = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
        return substr($ip, 0, 45);
    }

    private static function json(array $data, int $statusCode = 200): void
    {
        http_response_code($statusCode);
        header('Content-Type: application/json; charset=UTF-8');
        header('Cache-Control: no-store');
        echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        exit;
    }
}
