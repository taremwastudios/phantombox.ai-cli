<?php
/**
 * PhantomBox Core - Session service
 *
 * Owns the `sessions` table: bearer session tokens, expiry, revocation,
 * and the `profile_required` completion gate.
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Services;

use DateTimeImmutable;
use PDO;

class SessionService
{
    public const STATUS_ACTIVE = 'active';
    public const STATUS_REVOKED = 'revoked';

    public const LIFETIME_SECONDS = 86400; // 24 hours

    private PDO $db;

    public function __construct(?PDO $pdo = null)
    {
        $this->db = $pdo ?? Database::connect();
    }

    /**
     * Create a new session for a user.
     * `profile_required` sessions may only complete the profile; they must
     * never approve authorization requests (enforced in AuthorizationService).
     */
    public function create(int $userId, bool $profileRequired, ?string $clientType = null, ?string $ipAddress = null): array
    {
        $token = self::generateToken();
        $expiresAt = (new DateTimeImmutable('+' . self::LIFETIME_SECONDS . ' seconds'))
            ->format('Y-m-d H:i:s');

        $stmt = $this->db->prepare(
            'INSERT INTO sessions (token, user_id, status, profile_required, client_type, ip_address, created_at, expires_at)
             VALUES (?, ?, ?, ?, ?, ?, NOW(), ?)'
        );
        $stmt->execute([
            $token,
            $userId,
            self::STATUS_ACTIVE,
            $profileRequired ? 1 : 0,
            $clientType,
            $ipAddress,
            $expiresAt,
        ]);

        return [
            'token' => $token,
            'expires_at' => $expiresAt,
            'profile_required' => $profileRequired,
        ];
    }

    /**
     * Validate a bearer token. Returns the session row when the token is
     * active, unexpired and bound to an existing user; null otherwise.
     */
    public function validate(string $token): ?array
    {
        if ($token === '') {
            return null;
        }

        $stmt = $this->db->prepare(
            'SELECT s.*, u.public_id, u.email, u.first_name, u.last_name, u.phone_number, u.profile_completed
             FROM sessions s
             INNER JOIN users u ON u.id = s.user_id
             WHERE s.token = ?
               AND s.status = ?
               AND s.expires_at > NOW()
             LIMIT 1'
        );
        $stmt->execute([$token, self::STATUS_ACTIVE]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    /**
     * Revoke a session (sign-out). Idempotent.
     */
    public function revoke(string $token): bool
    {
        $stmt = $this->db->prepare(
            'UPDATE sessions SET status = ? WHERE token = ? AND status = ?'
        );
        $stmt->execute([self::STATUS_REVOKED, $token, self::STATUS_ACTIVE]);
        return $stmt->rowCount() > 0;
    }

    /**
     * Expire all sessions past their expiry time (maintenance).
     */
    public function purgeExpired(): int
    {
        $stmt = $this->db->prepare(
            "UPDATE sessions SET status = ? WHERE status = ? AND expires_at <= NOW()"
        );
        $stmt->execute([self::STATUS_REVOKED, self::STATUS_ACTIVE]);
        return $stmt->rowCount();
    }

    public function toUserArray(array $session): array
    {
        return [
            'public_id' => (string) $session['public_id'],
            'email' => (string) $session['email'],
            'first_name' => $session['first_name'] ?? null,
            'last_name' => $session['last_name'] ?? null,
            'phone_number' => $session['phone_number'] ?? null,
        ];
    }

    private static function generateToken(): string
    {
        return 'pbk_' . bin2hex(random_bytes(32));
    }
}
