<?php
/**
 * PhantomBox Core - Identity lifecycle controller
 *
 * Implements the existing Core contract consumed by PhantomBox Identity
 * (see phantombox-identity/src/Services/CoreApiClient.php):
 *   POST /v1/identity/email/request
 *   POST /v1/identity/email/verify
 *   POST /v1/identity/profile/complete
 *   GET  /v1/identity/profile
 *   POST /v1/identity/logout
 *
 * The client type header X-PhantomBox-Client is honored (defaults to web).
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Controllers;

use PhantomBox\Core\Services\EmailAuthService;
use PhantomBox\Core\Services\IdentityService;
use PhantomBox\Core\Services\SessionService;

class IdentityController
{
    private EmailAuthService $emailAuth;
    private IdentityService $identity;
    private SessionService $sessions;

    public function __construct()
    {
        $this->identity = new IdentityService();
        $this->sessions = new SessionService();
        $this->emailAuth = new EmailAuthService();
    }

    public function requestOtp(): void
    {
        $input = self::requestJson();
        $result = $this->emailAuth->requestCode((string) ($input['email'] ?? ''));
        if (!$result['ok']) {
            self::json($result, 422);
            return;
        }
        self::json(['ok' => true, 'message' => $result['message']], 200);
    }

    public function verifyOtp(): void
    {
        $input = self::requestJson();
        $result = $this->emailAuth->verifyCode(
            (string) ($input['email'] ?? ''),
            (string) ($input['code'] ?? ''),
            self::clientType(),
            $_SERVER['REMOTE_ADDR'] ?? null
        );
        if (!$result['ok']) {
            self::json($result, 401);
            return;
        }
        self::json($result, 200);
    }

    public function completeProfile(): void
    {
        $session = $this->requireSession();
        if ($session === null) {
            return; // response already sent
        }

        $input = self::requestJson();
        $firstName = trim((string) ($input['first_name'] ?? ''));
        $lastName = trim((string) ($input['last_name'] ?? ''));
        $phone = trim((string) ($input['phone_number'] ?? ''));

        if ($firstName === '' || $lastName === '') {
            self::json(['ok' => false, 'error' => 'First name and last name are required.'], 422);
            return;
        }

        $this->identity->completeProfile((int) $session['user_id'], $firstName, $lastName, $phone);
        $fresh = $this->identity->findById((int) $session['user_id']);

        self::json([
            'ok' => true,
            'status' => 'authenticated',
            'user' => $fresh === null ? null : $this->identity->toPublicArray($fresh),
        ], 200);
    }

    public function profile(): void
    {
        $session = $this->requireSession();
        if ($session === null) {
            return;
        }
        self::json([
            'ok' => true,
            'user' => $this->identity->toPublicArray($session),
            'profile_completed' => ((int) ($session['profile_completed'] ?? 0)) === 1,
        ], 200);
    }

    public function logout(): void
    {
        $token = self::bearerToken();
        if ($token !== null) {
            $this->sessions->revoke($token);
        }
        self::json(['ok' => true, 'message' => 'Signed out.'], 200);
    }

    // -------------------------------------------------------------------

    /**
     * Validate the bearer session and emit a 401 when absent/invalid.
     * Returns the session row or null after sending the error response.
     */
    private function requireSession(): ?array
    {
        $token = self::bearerToken();
        if ($token === null) {
            self::json(['ok' => false, 'error' => 'Authentication required.'], 401);
            return null;
        }
        $session = $this->sessions->validate($token);
        if ($session === null) {
            self::json(['ok' => false, 'error' => 'Authentication required or session expired.'], 401);
            return null;
        }
        return $session;
    }

    private static function clientType(): ?string
    {
        $raw = $_SERVER['HTTP_X_PHANTOMBOX_CLIENT'] ?? '';
        $value = strtolower(trim((string) $raw));
        return in_array($value, ['web', 'cli', 'app'], true) ? $value : null;
    }

    private static function requestJson(): array
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
        if (preg_match('/^\s*Bearer\s+(.+)$/i', $header, $m)) {
            return trim($m[1]);
        }
        return null;
    }

    private static function json(array $data, int $statusCode = 200): void
    {
        http_response_code($statusCode);
        header('Content-Type: application/json; charset=UTF-8');
        echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        exit;
    }
}
