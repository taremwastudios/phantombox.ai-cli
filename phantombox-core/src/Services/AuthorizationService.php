<?php
/**
 * PhantomBox Core - Shared Authorization Transaction Service
 *
 * ONE persistent authorization lifecycle shared by ALL client types
 * (web, cli, app). The client type is identified by the established
 * request header `X-PhantomBox-Client` — never by separate systems.
 *
 * Lifecycle:
 *   1. create()      - register a pending authorization transaction
 *   2. get()         - browser-facing transaction details (NO poll credential)
 *   3. approve()     - explicit decision by an authenticated user
 *   4. deny()        - explicit decision by an authenticated user
 *   5. poll()        - CLI/App polling with the separate polling credential
 *   6. expire()      - sweep expired pending transactions
 *   7. redeem()      - one-time secure credential delivery after approval
 *
 * Security invariants enforced here:
 *   - Creating a request authenticates/approves nothing.
 *   - Approving requires a valid, active, FULLY authenticated Core session
 *     (profile_required sessions are rejected).
 *   - Approval/denial is atomic (row-level guarded UPDATE) and
 *     idempotent-tolerant: conflicting or repeated decisions are rejected.
 *   - The polling credential is stored ONLY as a hash (never plaintext).
 *   - Credentials are issued exclusively on the approved state, exactly
 *     once, and only to the holder of the polling credential.
 *   - Pending/denied/expired transactions never return credentials.
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Services;

use DateTimeImmutable;
use PDO;
use PhantomBox\Core\Config\Config;
use RuntimeException;

class AuthorizationService
{
    public const STATE_PENDING = 'pending';
    public const STATE_APPROVED = 'approved';
    public const STATE_DENIED = 'denied';
    public const STATE_EXPIRED = 'expired';

    public const DEFAULT_SCOPE = 'Full CLI access (sessions, models, terminal tools)';

    private PDO $db;
    private SessionService $sessions;
    private IdentityService $identity;

    public function __construct(?PDO $pdo = null)
    {
        $this->db = $pdo ?? Database::connect();
        $this->sessions = new SessionService($this->db);
        $this->identity = new IdentityService($this->db);
    }

    // ---------------------------------------------------------------------
    // 1. Create
    // ---------------------------------------------------------------------

    /**
     * Create a pending authorization transaction.
     *
     * @param string $clientType  Resolved client type (web|cli|app)
     * @param array  $device      Device/client details (device_name, platform, version)
     * @param string $ipAddress   Requesting client IP (stored for browser display)
     * @param string|null $scope  Requested permissions/scope
     *
     * @return array{reference:string, poll_token:string, browser_url:string,
     *                expires_in:int, interval:int, status:string}
     *         The poll_token is returned ONCE here and never persisted in plaintext.
     */
    public function create(
        string $clientType,
        array $device,
        string $ipAddress,
        ?string $scope = null
    ): array {
        $reference = 'ref_' . bin2hex(random_bytes(10));
        $pollToken = 'poll_sec_' . bin2hex(random_bytes(16));
        $pollHash = password_hash($pollToken, PASSWORD_DEFAULT);
        $pollLookup = hash('sha256', $pollToken); // keyed lookup index (never the credential itself)

        // Identity-link secret: proves the browser link was issued by Core for
        // this transaction. Only its SHA-256 is stored; the plaintext is
        // returned once to the CLI and embedded in the browser URL.
        $identitySecret = 'idlink_' . bin2hex(random_bytes(24));
        $identityHash   = hash('sha256', $identitySecret);

        $ttl = max(60, (int) Config::get('authz_ttl', 600));
        $interval = max(2, (int) Config::get('authz_poll_interval', 5));
        $expiresAt = (new DateTimeImmutable('+' . $ttl . ' seconds'))->format('Y-m-d H:i:s');

        $stmt = $this->db->prepare(
            'INSERT INTO authorization_transactions
                (reference, client_type, device_name, device_platform, device_version,
                 ip_address, scope, poll_token_hash, poll_token_lookup, identity_link_hash,
                 status, credential_issued, created_at, expires_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NOW(), ?)'
        );
        $stmt->execute([
            $reference,
            $clientType,
            self::cleanDeviceField($device['device_name'] ?? null, 'Unknown device'),
            self::cleanDeviceField($device['platform'] ?? null, 'Unknown platform'),
            self::cleanDeviceField($device['cli_version'] ?? $device['version'] ?? null, ''),
            $ipAddress,
            self::normalizeScope($scope),
            $pollHash,
            $pollLookup,
            $identityHash,
            self::STATE_PENDING,
            $expiresAt,
        ]);

        return [
            'status' => self::STATE_PENDING,
            'reference' => $reference,
            'browser_url' => self::browserUrl($reference, $identitySecret),
            'poll_token' => $pollToken,
            'expires_in' => $ttl,
            'interval' => $interval,
        ];
    }

    // ---------------------------------------------------------------------
    // 2. Get (browser-facing details — NEVER includes the poll credential)
    // ---------------------------------------------------------------------

    /**
     * Transaction details for the browser authorization page.
     * The poll credential (plaintext or hash) is never included.
     *
     * @return array{transaction:array, found:bool, expired:bool}
     */
    public function get(string $reference): array
    {
        $row = $this->findByReference($reference);

        if ($row === null) {
            return ['found' => false, 'expired' => false, 'transaction' => null];
        }

        $expired = $row['status'] === self::STATE_PENDING && $this->isPastExpiry($row);
        if ($expired) {
            $this->markExpired($reference);
            $row['status'] = self::STATE_EXPIRED;
        }

        return [
            'found' => true,
            'expired' => $expired,
            'transaction' => self::toBrowserArray($row),
        ];
    }

    // ---------------------------------------------------------------------
    // 2b. Identity link validation (browser entry point)
    // ---------------------------------------------------------------------

    /**
     * Validate a CLI identity link before the authentication UI is rendered.
     *
     * The link is valid only when ALL of the following hold:
     *   - the reference and secret are well formed;
     *   - the secret's SHA-256 matches the stored identity_link_hash;
     *   - the transaction is still PENDING (not approved/denied/expired);
     *   - the server-side expiry has not passed (expiry is enforced here, not
     *     by the browser, and the browser never supplies a timestamp).
     *
     * The secret is never logged and never echoed back.
     *
     * @return array{valid:bool, reason:string, transaction:?array}
     */
    public function validateIdentityLink(string $reference, string $secret): array
    {
        $reference = trim($reference);
        $secret    = trim($secret);

        if ($reference === '' || $secret === '') {
            return self::linkInvalid('missing');
        }

        if (preg_match('/^[a-zA-Z0-9_\-\.:]{6,128}$/', $reference) !== 1
            || preg_match('/^idlink_[a-f0-9]{48}$/', $secret) !== 1) {
            return self::linkInvalid('malformed');
        }

        $row = $this->findByReference($reference);
        if ($row === null) {
            return self::linkInvalid('unknown');
        }

        $storedHash = (string) ($row['identity_link_hash'] ?? '');
        if ($storedHash === '' || !hash_equals($storedHash, hash('sha256', $secret))) {
            return self::linkInvalid('unknown');
        }

        // Server-side expiry: a pending row past its window is expired now.
        if ($row['status'] === self::STATE_PENDING && $this->isPastExpiry($row)) {
            $this->markExpired($reference);
            $row['status'] = self::STATE_EXPIRED;
        }

        if ($row['status'] !== self::STATE_PENDING) {
            return self::linkInvalid((string) $row['status']);
        }

        return [
            'valid'       => true,
            'reason'      => 'pending',
            'transaction' => self::toBrowserArray($row),
        ];
    }

    private static function linkInvalid(string $reason): array
    {
        return ['valid' => false, 'reason' => $reason, 'transaction' => null];
    }

    // ---------------------------------------------------------------------
    // 3. Approve (explicit, authenticated, atomic)
    // ---------------------------------------------------------------------

    /**
     * Approve a pending transaction on behalf of the authenticated user.
     *
     * @param string $sessionToken Bearer token of the approving Core session
     *
     * @return array{ok:bool, status:string, error?:string, code?:int}
     */
    public function approve(string $reference, string $sessionToken, string $clientType): array
    {
        $session = $this->requireApproverSession($sessionToken);
        // requireApproverSession() returns a structured error array on failure,
        // so the failure test is `ok === false`, not `is_array()`.
        if (($session['ok'] ?? true) === false) {
            return $session;
        }

        $row = $this->findByReference($reference);
        if ($row === null) {
            return self::fail('Authorization reference not found.', 404);
        }

        // The approving party acts through the web Identity app while the
        // transaction was created by the CLI, so the DECIDING request's client
        // type is intentionally NOT compared to the transaction's client_type.
        // The header is still required and validated by the controller.

        // Only pending transactions can be approved. Expired pending rows
        // transition to expired instead of approving.
        if ($row['status'] === self::STATE_PENDING && $this->isPastExpiry($row)) {
            $this->markExpired($reference);
            return self::fail('This authorization request has expired.', 410);
        }

        if ($row['status'] !== self::STATE_PENDING) {
            return self::fail('This authorization request has already been resolved.', 409);
        }

        // Atomic guarded transition: only one caller can move pending -> approved.
        $update = $this->db->prepare(
            'UPDATE authorization_transactions
             SET status = ?, user_id = ?, decided_at = NOW()
             WHERE reference = ? AND status = ?'
        );
        $update->execute([
            self::STATE_APPROVED,
            (int) $session['user_id'],
            $reference,
            self::STATE_PENDING,
        ]);

        if ($update->rowCount() === 0) {
            // Lost the race (denied/expired concurrently) — re-read truth.
            $fresh = $this->findByReference($reference);
            $current = $fresh['status'] ?? self::STATE_EXPIRED;
            return self::fail('This authorization request is already ' . $current . '.', 409);
        }

        return [
            'ok' => true,
            'status' => self::STATE_APPROVED,
            'message' => 'Device authorization granted.',
            'reference' => $reference,
        ];
    }

    // ---------------------------------------------------------------------
    // 4. Deny (explicit, authenticated, atomic)
    // ---------------------------------------------------------------------

    public function deny(string $reference, string $sessionToken, string $clientType): array
    {
        $session = $this->requireApproverSession($sessionToken);
        if (($session['ok'] ?? true) === false) {
            return $session;
        }

        $row = $this->findByReference($reference);
        if ($row === null) {
            return self::fail('Authorization reference not found.', 404);
        }

        // Same rationale as approve(): the deciding client type is not
        // compared to the transaction's client_type.

        if ($row['status'] === self::STATE_PENDING && $this->isPastExpiry($row)) {
            $this->markExpired($reference);
            return self::fail('This authorization request has expired.', 410);
        }

        if ($row['status'] !== self::STATE_PENDING) {
            return self::fail('This authorization request has already been resolved.', 409);
        }

        $update = $this->db->prepare(
            'UPDATE authorization_transactions
             SET status = ?, user_id = ?, decided_at = NOW()
             WHERE reference = ? AND status = ?'
        );
        $update->execute([
            self::STATE_DENIED,
            (int) $session['user_id'],
            $reference,
            self::STATE_PENDING,
        ]);

        if ($update->rowCount() === 0) {
            $fresh = $this->findByReference($reference);
            $current = $fresh['status'] ?? self::STATE_EXPIRED;
            return self::fail('This authorization request is already ' . $current . '.', 409);
        }

        return [
            'ok' => true,
            'status' => self::STATE_DENIED,
            'message' => 'Device authorization was rejected.',
            'reference' => $reference,
        ];
    }

    // ---------------------------------------------------------------------
    // 5. Poll (authenticates with the SEPARATE polling credential)
    // ---------------------------------------------------------------------

    /**
     * Poll transaction status using the polling credential (not the session
     * token, not the reference alone).
     *
     * @return array{ok:bool, status:string, http_code:int, ...credential fields on approved}
     */
    public function poll(string $pollToken): array
    {
        if ($pollToken === '') {
            return self::fail('Polling credential is required.', 401);
        }

        // Constant-time lookup by hash prefix is not possible with password_hash,
        // so transactions are located via a keyed lookup hash.
        $lookupHash = hash('sha256', $pollToken);
        $row = $this->findByPollLookup($lookupHash);

        if ($row === null) {
            return self::fail('Invalid polling credential.', 401);
        }

        // Verify the plaintext poll token against the stored hash (defence in
        // depth against hash-collision lookups).
        if (!password_verify($pollToken, (string) $row['poll_token_hash'])) {
            return self::fail('Invalid polling credential.', 401);
        }

        // Lazy expiry sweep for this transaction.
        if ($row['status'] === self::STATE_PENDING && $this->isPastExpiry($row)) {
            $this->markExpired((string) $row['reference']);
            $row['status'] = self::STATE_EXPIRED;
        }

        switch ($row['status']) {
            case self::STATE_PENDING:
                return [
                    'ok' => true,
                    'status' => self::STATE_PENDING,
                    'http_code' => 200,
                ];

            case self::STATE_DENIED:
                return [
                    'ok' => true,
                    'status' => self::STATE_DENIED,
                    'http_code' => 200,
                ];

            case self::STATE_EXPIRED:
                return [
                    'ok' => true,
                    'status' => self::STATE_EXPIRED,
                    'http_code' => 410,
                ];

            case self::STATE_APPROVED:
                // Credential delivery is a separate, one-time, authorized step.
                // Polling NEVER issues credentials by itself: the CLI must call
                // redeem() with the polling credential to take delivery.
                return [
                    'ok' => true,
                    'status' => self::STATE_APPROVED,
                    'http_code' => 200,
                    'reference' => (string) $row['reference'],
                    'redeem' => true,
                ];
        }

        return self::fail('Unknown authorization state.', 500);
    }

    // ---------------------------------------------------------------------
    // 6. Expire (scheduled sweep)
    // ---------------------------------------------------------------------

    /**
     * Expire all pending transactions past their expiry window.
     *
     * @return int Number of transactions transitioned to expired.
     */
    public function expire(): int
    {
        $stmt = $this->db->prepare(
            'UPDATE authorization_transactions
             SET status = ?
             WHERE status = ? AND expires_at <= NOW()'
        );
        $stmt->execute([self::STATE_EXPIRED, self::STATE_PENDING]);
        return $stmt->rowCount();
    }

    // ---------------------------------------------------------------------
    // 7. Redeem (secure one-time credential delivery after approval)
    // ---------------------------------------------------------------------

    /**
     * Deliver the approved session credentials to the polling client.
     *
     * Requirements enforced:
     *   - Polling credential must verify.
     *   - Transaction must be in the APPROVED state (never pending/denied/expired).
     *   - Delivery happens exactly once (credential_issued guard) — replay of
     *     the redemption step returns 409 without re-issuing anything.
     *
     * @return array{ok:bool, status:string, http_code:int, token?:string, user?:array, expires_at?:string}
     */
    public function redeem(string $pollToken): array
    {
        if ($pollToken === '') {
            return self::fail('Polling credential is required.', 401);
        }

        $lookupHash = hash('sha256', $pollToken);
        $row = $this->findByPollLookup($lookupHash);

        if ($row === null || !password_verify($pollToken, (string) $row['poll_token_hash'])) {
            return self::fail('Invalid polling credential.', 401);
        }

        if ($row['status'] !== self::STATE_APPROVED) {
            // Pending, denied, expired: never return credentials.
            return self::fail(
                'Credentials are only available for approved authorizations (current state: '
                . (string) $row['status'] . ').',
                403
            );
        }

        if (((int) $row['credential_issued']) === 1) {
            return self::fail('Credentials have already been delivered for this authorization.', 409);
        }

        // Create a real Core session for the approving user and deliver it once.
        $session = $this->sessions->create(
            (int) $row['user_id'],
            false,
            (string) $row['client_type'],
            (string) ($row['ip_address'] ?? null)
        );

        $guard = $this->db->prepare(
            'UPDATE authorization_transactions
             SET credential_issued = 1, credential_issued_at = NOW()
             WHERE reference = ? AND credential_issued = 0 AND status = ?'
        );
        $guard->execute([(string) $row['reference'], self::STATE_APPROVED]);

        if ($guard->rowCount() === 0) {
            // Concurrent redemption already delivered the credentials.
            // Revoke the duplicate session we just created.
            $this->sessions->revoke($session['token']);
            return self::fail('Credentials have already been delivered for this authorization.', 409);
        }

        $user = $this->identity->findById((int) $row['user_id']);
        if ($user === null) {
            return self::fail('Approving user no longer exists.', 410);
        }

        return [
            'ok' => true,
            'status' => self::STATE_APPROVED,
            'http_code' => 200,
            'token' => $session['token'],
            'expires_at' => $session['expires_at'],
            'user' => $this->identity->toPublicArray($user),
        ];
    }

    // ---------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------

    /**
     * Validate the approving session. Enforces:
     *   - Bearer token present and valid.
     *   - Session is active and unexpired (SessionService::validate).
     *   - Session is FULLY authenticated — profile_required sessions are
     *     rejected (Section 5: profile-completion sessions must not approve).
     */
    private function requireApproverSession(string $sessionToken): array
    {
        $token = trim($sessionToken);
        if ($token === '') {
            return self::fail('Authentication required.', 401);
        }

        $session = $this->sessions->validate($token);
        if ($session === null) {
            return self::fail('Authentication required or session expired.', 401);
        }

        $profileRequired = ((int) ($session['profile_required'] ?? 0)) === 1
            || ((int) ($session['profile_completed'] ?? 1)) !== 1;

        if ($profileRequired) {
            return self::fail('Complete your profile before approving device authorizations.', 403);
        }

        return $session;
    }

    private function findByReference(string $reference): ?array
    {
        $stmt = $this->db->prepare(
            'SELECT * FROM authorization_transactions WHERE reference = ? LIMIT 1'
        );
        $stmt->execute([$reference]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    private function findByPollLookup(string $lookupHash): ?array
    {
        $stmt = $this->db->prepare(
            'SELECT * FROM authorization_transactions WHERE poll_token_lookup = ? LIMIT 1'
        );
        $stmt->execute([$lookupHash]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    private function isPastExpiry(array $row): bool
    {
        $expiresAt = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', (string) $row['expires_at']);
        if ($expiresAt === false) {
            return true;
        }
        return $expiresAt->getTimestamp() <= time();
    }

    private function markExpired(string $reference): void
    {
        $stmt = $this->db->prepare(
            'UPDATE authorization_transactions SET status = ?
             WHERE reference = ? AND status = ?'
        );
        $stmt->execute([self::STATE_EXPIRED, $reference, self::STATE_PENDING]);
    }

    private static function clientTypeMismatch(array $row, string $clientType): bool
    {
        return $clientType !== '' && (string) $row['client_type'] !== $clientType;
    }

    private static function fail(string $message, int $code): array
    {
        return ['ok' => false, 'error' => $message, 'code' => $code];
    }

    /**
     * Browser link for the CLI device authorization page.
     *
     * Carries the public reference (for display) and the identity-link secret
     * (for server-side validation). The secret is what authorizes the page.
     */
    private static function browserUrl(string $reference, string $identitySecret): string
    {
        $base = (string) Config::get('identity_app_url', 'https://phantomboxai.phantom-illusions-studio.com');
        return $base . '/sign-in/cli/identity'
            . '?id=' . rawurlencode($reference)
            . '&k=' . rawurlencode($identitySecret);
    }

    private static function normalizeScope(?string $scope): string
    {
        $scope = trim((string) $scope);
        if ($scope === '') {
            $scope = self::DEFAULT_SCOPE;
        }
        // Keep the scope a single line, bounded length, no control characters.
        $scope = preg_replace('/[\x00-\x1F\x7F]+/u', ' ', $scope) ?? self::DEFAULT_SCOPE;
        return mb_substr($scope, 0, 255);
    }

    private static function cleanDeviceField(mixed $value, string $default): string
    {
        $value = trim((string) $value);
        $value = preg_replace('/[\x00-\x1F\x7F]+/u', ' ', $value) ?? '';
        if ($value === '') {
            return $default;
        }
        return mb_substr($value, 0, 255);
    }

    /**
     * Browser-safe projection of a transaction row. Deliberately excludes
     * poll_token_hash, poll_token_lookup, credential_issued* and user_id.
     */
    private static function toBrowserArray(array $row): array
    {
        return [
            'reference' => (string) $row['reference'],
            'status' => (string) $row['status'],
            'client_type' => (string) $row['client_type'],
            'device_name' => (string) $row['device_name'],
            'platform' => (string) $row['device_platform'],
            'ip_address' => (string) $row['ip_address'],
            'scope' => (string) $row['scope'],
            'requested_at' => (string) $row['created_at'],
            'expires_at' => (string) $row['expires_at'],
        ];
    }
}
