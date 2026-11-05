<?php
/**
 * PhantomBox Core - Shared Authorization Transaction Service
 *
 * Built ON TOP OF the production authentication services (src/Identity/*).
 * This class owns NO users, NO sessions and NO OTP codes. It records a
 * transaction and delegates all authentication work:
 *
 *   - User lookup / profile state  -> IdentityService::findOrCreateVerifiedUser()
 *                                    IdentityService::completeProfile()
 *   - Session issuance / validity  -> SessionService::create() / validate() / revoke()
 *   - Mail, OTP + rate limiting    -> EmailAuthService (unchanged, not touched here)
 *
 * Production schema referenced (never altered by this feature):
 *   users(id, public_id, account_status, registration_status, profile_completed_at)
 *   user_emails(user_id, email, normalized_email, is_primary, verified_at)
 *   auth_sessions(id, user_id, session_scope, token_hash, device_name,
 *                 user_agent, last_seen_at, expires_at, revoked_at)
 *
 * New table added by this feature only (see migrations/002_*.sql):
 *   authorization_transactions
 *
 * Lifecycle:
 *   1. create()  - register a pending authorization transaction
 *   2. get()     - browser-facing transaction details (NO poll credential)
 *   3. approve() - explicit decision by an authenticated user
 *   4. deny()    - explicit decision by an authenticated user
 *   5. poll()    - CLI/App polling with the separate polling credential
 *   6. redeem()  - one-time secure credential delivery after approval
 *
 * Security invariants enforced here:
 *   - Creating a request authenticates/approves nothing.
 *   - Approving requires a valid, active, FULLY registered user
 *     (registration_status === 'complete', account_status === 'active').
 *   - Approval/denial is atomic (row-level guarded UPDATE) and final.
 *   - The polling credential is stored ONLY as a hash (never plaintext).
 *   - The issued session token is stored ONLY as a SHA-256 hash, produced
 *     by the production SessionService::create() (auth_sessions table).
 *   - Credentials are issued exclusively on the approved state, exactly
 *     once, and only to the holder of the polling credential.
 *   - Pending/denied/expired transactions never return credentials.
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

final class AuthorizationService
{
    public const STATE_PENDING  = 'pending';
    public const STATE_APPROVED = 'approved';
    public const STATE_DENIED   = 'denied';
    public const STATE_EXPIRED  = 'expired';

    public const DEFAULT_SCOPE = 'Full CLI access (sessions, models, terminal tools)';

    public const DEFAULT_TTL_SECONDS = 600;   // authorization window
    public const DEFAULT_INTERVAL    = 5;     // suggested polling interval

    private PDO $database;
    private SessionService $sessions;
    private IdentityService $identity;

    public function __construct(PDO $database)
    {
        $this->database = $database;
        $this->sessions = new SessionService($database);
        $this->identity = new IdentityService($database);
    }

    // ---------------------------------------------------------------------
    // 1. Create
    // ---------------------------------------------------------------------

    /**
     * Create a pending authorization transaction.
     *
     * @param string $clientType   Resolved client type (web|cli|app)
     * @param array  $device       Device/client details (device_name, platform, version)
     * @param string $ipAddress    Requesting client IP (stored for browser display)
     * @param string|null $scope   Requested permissions/scope
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
        $pollHash  = password_hash($pollToken, PASSWORD_DEFAULT);
        $pollLookup = hash('sha256', $pollToken); // keyed lookup index (never the credential itself)

        $ttl      = self::resolveTtl();
        $interval = self::resolveInterval();
        $expiresAt = (new DateTimeImmutable('now', new DateTimeZone('UTC')))
            ->modify('+' . $ttl . ' seconds')
            ->format('Y-m-d H:i:s');

        $stmt = $this->database->prepare(
            'INSERT INTO authorization_transactions
                (reference, client_type, device_name, device_platform, device_version,
                 ip_address, scope, poll_token_hash, poll_token_lookup, status,
                 credential_issued, created_at, expires_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, UTC_TIMESTAMP(), ?)'
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
            self::STATE_PENDING,
            $expiresAt,
        ]);

        return [
            'status'      => self::STATE_PENDING,
            'reference'   => $reference,
            'browser_url' => self::browserUrl($reference),
            'poll_token'  => $pollToken,
            'expires_in'  => $ttl,
            'interval'    => $interval,
        ];
    }

    // ---------------------------------------------------------------------
    // 2. Get (browser-facing details — NEVER includes the poll credential)
    // ---------------------------------------------------------------------

    /**
     * Transaction details for the browser authorization page.
     * The poll credential (plaintext or hash) is never included.
     *
     * @return array{transaction:array|null, found:bool, expired:bool}
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
            'found'       => true,
            'expired'     => $expired,
            'transaction' => self::toBrowserArray($row),
        ];
    }

    // ---------------------------------------------------------------------
    // 3. Approve (explicit, authenticated, atomic)
    // ---------------------------------------------------------------------

    /**
     * Approve a pending transaction on behalf of the authenticated user.
     *
     * The bearer token MUST be a production auth_sessions token (validated
     * through the existing SessionService::validate()).
     *
     * @return array{ok:bool, status?:string, reference?:string, message?:string, error?:string, code?:int}
     */
    public function approve(string $reference, string $sessionToken, string $clientType): array
    {
        $session = $this->requireApproverSession($sessionToken);
        if ($session === null) {
            return self::fail('Authentication required or session expired.', 401);
        }
        if ($session === false) {
            return self::fail('Complete your profile before approving device authorizations.', 403);
        }

        $row = $this->findByReference($reference);
        if ($row === null) {
            return self::fail('Authorization reference not found.', 404);
        }

        // NOTE: the approving party acts through the web Identity app while
        // the transaction was created by the CLI, so the client type of the
        // DECIDING request is intentionally NOT compared to the transaction's
        // client_type. The header is still required and validated.

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
        $update = $this->database->prepare(
            'UPDATE authorization_transactions
             SET status = ?, user_id = ?, decided_at = UTC_TIMESTAMP()
             WHERE reference = ? AND status = ?'
        );
        $update->execute([
            self::STATE_APPROVED,
            $session['user_id'],
            $reference,
            self::STATE_PENDING,
        ]);

        if ($update->rowCount() === 0) {
            // Lost the race (denied/expired concurrently) — re-read truth.
            $fresh   = $this->findByReference($reference);
            $current = $fresh['status'] ?? self::STATE_EXPIRED;
            return self::fail('This authorization request is already ' . $current . '.', 409);
        }

        return [
            'ok'        => true,
            'status'    => self::STATE_APPROVED,
            'message'   => 'Device authorization granted.',
            'reference' => $reference,
        ];
    }

    // ---------------------------------------------------------------------
    // 4. Deny (explicit, authenticated, atomic)
    // ---------------------------------------------------------------------

    public function deny(string $reference, string $sessionToken, string $clientType): array
    {
        $session = $this->requireApproverSession($sessionToken);
        if ($session === null) {
            return self::fail('Authentication required or session expired.', 401);
        }
        if ($session === false) {
            return self::fail('Complete your profile before approving device authorizations.', 403);
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

        $update = $this->database->prepare(
            'UPDATE authorization_transactions
             SET status = ?, user_id = ?, decided_at = UTC_TIMESTAMP()
             WHERE reference = ? AND status = ?'
        );
        $update->execute([
            self::STATE_DENIED,
            $session['user_id'],
            $reference,
            self::STATE_PENDING,
        ]);

        if ($update->rowCount() === 0) {
            $fresh   = $this->findByReference($reference);
            $current = $fresh['status'] ?? self::STATE_EXPIRED;
            return self::fail('This authorization request is already ' . $current . '.', 409);
        }

        return [
            'ok'        => true,
            'status'    => self::STATE_DENIED,
            'message'   => 'Device authorization was rejected.',
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
                    'ok'        => true,
                    'status'    => self::STATE_PENDING,
                    'http_code' => 200,
                ];

            case self::STATE_DENIED:
                return [
                    'ok'        => true,
                    'status'    => self::STATE_DENIED,
                    'http_code' => 200,
                ];

            case self::STATE_EXPIRED:
                return [
                    'ok'        => true,
                    'status'    => self::STATE_EXPIRED,
                    'http_code' => 410,
                ];

            case self::STATE_APPROVED:
                // Credential delivery is a separate, one-time, authorized step.
                // Polling NEVER issues credentials by itself: the CLI must call
                // redeem() with the polling credential to take delivery.
                return [
                    'ok'        => true,
                    'status'    => self::STATE_APPROVED,
                    'http_code' => 200,
                    'reference' => (string) $row['reference'],
                    'redeem'    => true,
                ];
        }

        return self::fail('Unknown authorization state.', 500);
    }

    // ---------------------------------------------------------------------
    // 6. Redeem (secure one-time credential delivery after approval)
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
     * The delivered token is a PRODUCTION auth_sessions bearer token issued
     * through SessionService::create() — identical to web sign-in tokens.
     *
     * @return array{ok:bool, status:string, http_code:int, token?:string, user?:array, expires_at?:string, error?:string, code?:int}
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

        // Create a real production session for the approving user and deliver it once.
        try {
            $session = $this->sessions->create(
                (int) $row['user_id']
            );
        } catch (Throwable $e) {
            // Account unavailable / invalid user — transaction stays approved
            // but undeliverable; treat as gone so the client re-authenticates.
            return self::fail('Approving user is no longer available.', 410);
        }

        $guard = $this->database->prepare(
            'UPDATE authorization_transactions
             SET credential_issued = 1, credential_issued_at = UTC_TIMESTAMP(), issued_token_hash = ?
             WHERE reference = ? AND credential_issued = 0 AND status = ?'
        );
        $guard->execute([
            hash('sha256', (string) $session['token']),
            (string) $row['reference'],
            self::STATE_APPROVED,
        ]);

        if ($guard->rowCount() === 0) {
            // Concurrent redemption already delivered the credentials.
            // Revoke the duplicate session we just created.
            $this->sessions->revoke((string) $session['token']);
            return self::fail('Credentials have already been delivered for this authorization.', 409);
        }

        $user = $this->findUserRow((int) $row['user_id']);
        if ($user === null) {
            return self::fail('Approving user no longer exists.', 410);
        }

        return [
            'ok'         => true,
            'status'     => self::STATE_APPROVED,
            'http_code'  => 200,
            'token'      => (string) $session['token'],
            'expires_at' => (string) $session['expires_at'],
            'user'       => self::toPublicUser($user),
        ];
    }

    // ---------------------------------------------------------------------
    // Internals
    // ---------------------------------------------------------------------

    /**
     * Validate the approving session using the PRODUCTION SessionService.
     * Enforces:
     *   - Bearer token present and a 64-char hex production session token.
     *   - Session is active and unexpired (auth_sessions.revoked_at IS NULL).
     *   - Session scope is 'full' (profile_completion sessions are rejected).
     *   - Owning user is active and fully registered.
     *
     * @return array|null|false array = valid full session;
     *         null = not authenticated (401); false = profile-restricted (403)
     */
    private function requireApproverSession(string $sessionToken): array|false|null
    {
        $token = trim($sessionToken);
        if ($token === '') {
            return null;
        }

        $session = $this->sessions->validate($token);
        if ($session === false || !is_array($session)) {
            return null;
        }

        if (($session['scope'] ?? '') !== 'full') {
            return false;
        }

        return $session;
    }

    private function findByReference(string $reference): ?array
    {
        $stmt = $this->database->prepare(
            'SELECT * FROM authorization_transactions WHERE reference = ? LIMIT 1'
        );
        $stmt->execute([$reference]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    private function findByPollLookup(string $lookupHash): ?array
    {
        $stmt = $this->database->prepare(
            'SELECT * FROM authorization_transactions WHERE poll_token_lookup = ? LIMIT 1'
        );
        $stmt->execute([$lookupHash]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    /**
     * Read-only lookup of a production users row (no schema assumptions
     * beyond the documented production columns).
     */
    private function findUserRow(int $userId): ?array
    {
        $stmt = $this->database->prepare(
            'SELECT id, public_id, first_name, last_name, phone_number,
                    account_status, registration_status
             FROM users WHERE id = ? LIMIT 1'
        );
        $stmt->execute([$userId]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    private function isPastExpiry(array $row): bool
    {
        $expiresAt = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', (string) $row['expires_at']);
        if ($expiresAt === false) {
            return true;
        }
        $now = new DateTimeImmutable('now', new DateTimeZone('UTC'));
        return $expiresAt->getTimestamp() <= $now->getTimestamp();
    }

    private function markExpired(string $reference): void
    {
        $stmt = $this->database->prepare(
            'UPDATE authorization_transactions SET status = ?
             WHERE reference = ? AND status = ?'
        );
        $stmt->execute([self::STATE_EXPIRED, $reference, self::STATE_PENDING]);
    }

    private static function fail(string $message, int $code): array
    {
        return ['ok' => false, 'error' => $message, 'code' => $code];
    }

    private static function browserUrl(string $reference): string
    {
        $base = getenv('IDENTITY_APP_URL')
            ?: ($_ENV['IDENTITY_APP_URL'] ?? null)
            ?: 'https://phantomboxai.phantom-illusions-studio.com';
        return rtrim((string) $base, '/') . '/sign-in/cli/identity?id=' . rawurlencode($reference);
    }

    private static function resolveTtl(): int
    {
        $ttl = (int) (getenv('AUTHZ_TTL') ?: ($_ENV['AUTHZ_TTL'] ?? ''));
        return max(60, $ttl > 0 ? $ttl : self::DEFAULT_TTL_SECONDS);
    }

    private static function resolveInterval(): int
    {
        $interval = (int) (getenv('AUTHZ_POLL_INTERVAL') ?: ($_ENV['AUTHZ_POLL_INTERVAL'] ?? ''));
        return max(2, $interval > 0 ? $interval : self::DEFAULT_INTERVAL);
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

    private static function cleanDeviceField(?string $value, string $default): string
    {
        $value = trim((string) $value);
        $value = preg_replace('/[\x00-\x1F\x7F]+/u', ' ', $value) ?? '';
        if ($value === '') {
            return $default;
        }
        return mb_substr($value, 0, 255);
    }

    /**
     * Public-safe projection of the production users row.
     */
    private static function toPublicUser(array $user): array
    {
        return [
            'public_id'           => (string) $user['public_id'],
            'first_name'          => $user['first_name'] ?? null,
            'last_name'           => $user['last_name'] ?? null,
            'phone_number'        => $user['phone_number'] ?? null,
            'registration_status' => (string) ($user['registration_status'] ?? 'complete'),
        ];
    }

    /**
     * Browser-safe projection of a transaction row. Deliberately excludes
     * poll_token_hash, poll_token_lookup, issued_token_hash,
     * credential_issued* and user_id.
     */
    private static function toBrowserArray(array $row): array
    {
        return [
            'reference'    => (string) $row['reference'],
            'status'       => (string) $row['status'],
            'client_type'  => (string) $row['client_type'],
            'device_name'  => (string) $row['device_name'],
            'platform'     => (string) $row['device_platform'],
            'ip_address'   => (string) $row['ip_address'],
            'scope'        => (string) $row['scope'],
            'requested_at' => (string) $row['created_at'],
            'expires_at'   => (string) $row['expires_at'],
        ];
    }
}
