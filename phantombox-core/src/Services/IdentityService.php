<?php
/**
 * PhantomBox Core - User identity and registration service
 *
 * Owns the `users` table: user records, public identifiers, and profile state.
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Services;

use PDO;

class IdentityService
{
    private PDO $db;

    public function __construct(?PDO $pdo = null)
    {
        $this->db = $pdo ?? Database::connect();
    }

    /**
     * Find a user by email (case-insensitive). Returns null when absent.
     */
    public function findByEmail(string $email): ?array
    {
        $stmt = $this->db->prepare('SELECT * FROM users WHERE email = ? LIMIT 1');
        $stmt->execute([strtolower(trim($email))]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    public function findById(int $id): ?array
    {
        $stmt = $this->db->prepare('SELECT * FROM users WHERE id = ? LIMIT 1');
        $stmt->execute([$id]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    /**
     * Get a user by public identifier (never exposes the internal id).
     */
    public function findByPublicId(string $publicId): ?array
    {
        $stmt = $this->db->prepare('SELECT * FROM users WHERE public_id = ? LIMIT 1');
        $stmt->execute([$publicId]);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    /**
     * Register a new user or return the existing one (idempotent on email).
     * New users start with `profile_completed = 0`.
     */
    public function register(string $email): array
    {
        $existing = $this->findByEmail($email);
        if ($existing !== null) {
            return $existing;
        }

        $stmt = $this->db->prepare(
            'INSERT INTO users (public_id, email, profile_completed, created_at)
             VALUES (?, ?, 0, NOW())'
        );
        $stmt->execute([self::generatePublicId(), strtolower(trim($email))]);

        return $this->findByEmail($email) ?? throw new \RuntimeException('User registration failed.');
    }

    /**
     * Complete the user profile. Returns true when a row was updated.
     */
    public function completeProfile(int $userId, string $firstName, string $lastName, string $phoneNumber): bool
    {
        $stmt = $this->db->prepare(
            'UPDATE users
             SET first_name = ?, last_name = ?, phone_number = ?, profile_completed = 1
             WHERE id = ?'
        );
        $stmt->execute([trim($firstName), trim($lastName), trim($phoneNumber), $userId]);
        return $stmt->rowCount() > 0;
    }

    /**
     * Public-safe representation of a user (no internal ids or secrets).
     */
    public function toPublicArray(array $user): array
    {
        return [
            'public_id' => (string) $user['public_id'],
            'email' => (string) $user['email'],
            'first_name' => $user['first_name'] ?? null,
            'last_name' => $user['last_name'] ?? null,
            'phone_number' => $user['phone_number'] ?? null,
            'profile_completed' => (int) ($user['profile_completed'] ?? 0) === 1,
        ];
    }

    private static function generatePublicId(): string
    {
        return 'usr_' . bin2hex(random_bytes(8));
    }
}
