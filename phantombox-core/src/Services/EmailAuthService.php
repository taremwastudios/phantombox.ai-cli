<?php
/**
 * PhantomBox Core - Email authentication service (passwordless OTP)
 *
 * Owns the `email_otp_codes` table. OTP codes are stored hashed,
 * expire quickly and are single-use.
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Services;

use PDO;

class EmailAuthService
{
    private const CODE_TTL_SECONDS = 600;   // 10 minutes
    private const MAX_ATTEMPTS = 5;

    private PDO $db;
    private IdentityService $identity;

    public function __construct(?PDO $pdo = null, ?IdentityService $identity = null)
    {
        $this->db = $pdo ?? Database::connect();
        $this->identity = $identity ?? new IdentityService($this->db);
    }

    /**
     * Request an OTP for an email. Always creates (or reuses) the user and
     * returns true. The response never reveals whether the email exists.
     */
    public function requestCode(string $email): array
    {
        $email = strtolower(trim($email));

        if (!filter_var($email, FILTER_VALIDATE_EMAIL)) {
            return ['ok' => false, 'error' => 'A valid email address is required.'];
        }

        $user = $this->identity->register($email);

        // Invalidate previous outstanding codes for this user.
        $invalidate = $this->db->prepare(
            'UPDATE email_otp_codes SET consumed = 1 WHERE user_id = ? AND consumed = 0'
        );
        $invalidate->execute([(int) $user['id']]);

        $code = str_pad((string) random_int(0, 999999), 6, '0', STR_PAD_LEFT);
        $codeHash = password_hash($code, PASSWORD_DEFAULT);
        $expiresAt = date('Y-m-d H:i:s', time() + self::CODE_TTL_SECONDS);

        $stmt = $this->db->prepare(
            'INSERT INTO email_otp_codes (user_id, code_hash, attempts, consumed, expires_at, created_at)
             VALUES (?, ?, 0, 0, ?, NOW())'
        );
        $stmt->execute([(int) $user['id'], $codeHash, $expiresAt]);

        // Deliver the code through the configured mail transport. In
        // production this is delegated to the platform mailer; the code is
        // never returned in the HTTP response.
        self::sendOtpEmail($email, $code);

        return [
            'ok' => true,
            'message' => 'Verification code sent',
            'profile_required' => ((int) $user['profile_completed']) !== 1,
        ];
    }

    /**
     * Verify an OTP code. On success creates a session for the user.
     */
    public function verifyCode(string $email, string $code, ?string $clientType = null, ?string $ipAddress = null): array
    {
        $email = strtolower(trim($email));
        $code = trim($code);

        $user = $this->identity->findByEmail($email);
        if ($user === null) {
            return ['ok' => false, 'error' => 'Invalid email or verification code.'];
        }

        $stmt = $this->db->prepare(
            'SELECT * FROM email_otp_codes
             WHERE user_id = ? AND consumed = 0 AND expires_at > NOW()
             ORDER BY id DESC LIMIT 1'
        );
        $stmt->execute([(int) $user['id']]);
        $otp = $stmt->fetch();

        if ($otp === false) {
            return ['ok' => false, 'error' => 'Invalid email or verification code.'];
        }

        if ((int) $otp['attempts'] >= self::MAX_ATTEMPTS) {
            $consume = $this->db->prepare('UPDATE email_otp_codes SET consumed = 1 WHERE id = ?');
            $consume->execute([(int) $otp['id']]);
            return ['ok' => false, 'error' => 'Too many attempts. Request a new verification code.'];
        }

        $bump = $this->db->prepare('UPDATE email_otp_codes SET attempts = attempts + 1 WHERE id = ?');
        $bump->execute([(int) $otp['id']]);

        if (!password_verify($code, (string) $otp['code_hash'])) {
            return ['ok' => false, 'error' => 'Invalid email or verification code.'];
        }

        // Single use: consume atomically so a replayed code fails.
        $consume = $this->db->prepare(
            'UPDATE email_otp_codes SET consumed = 1 WHERE id = ? AND consumed = 0'
        );
        $consume->execute([(int) $otp['id']]);
        if ($consume->rowCount() === 0) {
            return ['ok' => false, 'error' => 'Invalid email or verification code.'];
        }

        $profileRequired = ((int) $user['profile_completed']) !== 1;
        $sessions = new SessionService($this->db);
        $session = $sessions->create((int) $user['id'], $profileRequired, $clientType, $ipAddress);

        return [
            'ok' => true,
            'status' => $profileRequired ? 'profile_required' : 'authenticated',
            'token' => $session['token'],
            'expires_at' => $session['expires_at'],
            'user' => $this->identity->toPublicArray($user),
        ];
    }

    /**
     * Mail transport hook. Uses the platform mail() relay when available so
     * the OTP reaches the user's inbox; failures are logged, never echoed.
     */
    private static function sendOtpEmail(string $email, string $code): void
    {
        $subject = 'Your PhantomBox verification code';
        $body = "Your PhantomBox verification code is: {$code}\n\n"
            . "This code expires in 10 minutes. If you did not request it, ignore this email.\n";
        $headers = 'From: no-reply@phantom-illusions-studio.com';

        if (!@mail($email, $subject, $body, $headers)) {
            error_log('PhantomBox Core: failed to queue OTP email for ' . hash('sha256', $email));
        }
    }
}
