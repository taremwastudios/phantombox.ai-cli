<?php
/**
 * PhantomBox Core - Device authorization endpoint tests
 *
 * Phantom Illusions Studio
 *
 * Usage:
 *   php tests/authorization_endpoints_test.php          # against local MariaDB/MySQL
 *   php tests/authorization_endpoints_test.php --sqlite # logic-only, SQLite fallback
 *
 * SQLite mode exercises the full controller/service flow using production
 * SessionService + IdentityService SQL semantics, adapted for SQLite
 * (UTC_TIMESTAMP() -> strftime, FOR UPDATE skipped, TIMESTAMP -> TEXT).
 *
 * The suite calls the service layer directly (not via HTTP) and asserts
 * create -> poll -> approve -> poll -> redeem, deny, expiry and
 * one-time redemption guarantees.
 */

declare(strict_types=1);

error_reporting(E_ALL);
ini_set('display_errors', '1');

$root = dirname(__DIR__);
$prodRoot = dirname(__DIR__, 2) . '/production-reference/phantombox-core';

require_once $prodRoot . '/src/Identity/IdentityService.php';
require_once $prodRoot . '/src/Identity/SessionService.php';
require_once $root . '/src/Authorization/AuthorizationService.php';

$useSqlite = in_array('--sqlite', $argv ?? [], true);

// ---------------------------------------------------------------------
// Database bootstrap
// ---------------------------------------------------------------------

function makeDatabase(bool $sqlite): PDO
{
    if ($sqlite) {
        $pdo = new PDO('sqlite::memory:');
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $pdo->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);

        $pdo->sqliteCreateFunction('UTC_TIMESTAMP', static function (): string {
            return gmdate('Y-m-d H:i:s');
        });

        $pdo->exec('
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL,
                account_status TEXT NOT NULL DEFAULT "active",
                registration_status TEXT NOT NULL DEFAULT "pending_profile",
                first_name TEXT NULL,
                last_name TEXT NULL,
                phone_number TEXT NULL,
                profile_completed_at TEXT NULL
            )
        ');
        $pdo->exec('
            CREATE TABLE user_emails (
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                normalized_email TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 1,
                verified_at TEXT NULL
            )
        ');
        $pdo->exec('
            CREATE TABLE auth_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_scope TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                device_name TEXT NULL,
                user_agent TEXT NULL,
                last_seen_at TEXT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT NULL
            )
        ');
        $pdo->exec('
            CREATE TABLE authorization_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL UNIQUE,
                client_type TEXT NOT NULL,
                device_name TEXT NOT NULL,
                device_platform TEXT NOT NULL,
                device_version TEXT NOT NULL DEFAULT "",
                ip_address TEXT NOT NULL DEFAULT "",
                scope TEXT NOT NULL,
                poll_token_hash TEXT NOT NULL,
                poll_token_lookup TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT "pending",
                user_id INTEGER NULL,
                credential_issued INTEGER NOT NULL DEFAULT 0,
                credential_issued_at TEXT NULL,
                issued_token_hash TEXT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT NULL,
                expires_at TEXT NOT NULL
            )
        ');
        return $pdo;
    }

    // MariaDB/MySQL mode: expects a test database with the production
    // schema plus migrations/002_create_authorization_transactions.sql applied.
    $host = getenv('TEST_DB_HOST') ?: '127.0.0.1';
    $port = getenv('TEST_DB_PORT') ?: '3306';
    $name = getenv('TEST_DB_NAME') ?: 'phantombox_core_test';
    $user = getenv('TEST_DB_USER') ?: 'root';
    $pass = getenv('TEST_DB_PASS') ?: '';

    $pdo = new PDO(
        "mysql:host={$host};port={$port};dbname={$name};charset=utf8mb4",
        $user,
        $pass,
        [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES   => false,
        ]
    );

    // Ensure the authorization table exists (idempotent).
    $pdo->exec(str_replace(
        ['CREATE TABLE IF NOT EXISTS `authorization_transactions`', 'CREATE TABLE IF NOT EXISTS `authorization_transactions` ('],
        ['CREATE TABLE IF NOT EXISTS `authorization_transactions`', 'CREATE TABLE IF NOT EXISTS `authorization_transactions` ('],
        file_get_contents($root . '/migrations/002_create_authorization_transactions.sql')
    ));

    return $pdo;
}

// ---------------------------------------------------------------------
// Test runner (no PHPUnit dependency required)
// ---------------------------------------------------------------------

$tests = [];
$failures = 0;

function test(string $name, callable $fn): void
{
    global $tests, $failures;
    try {
        $fn();
        echo "  [PASS] {$name}\n";
        $tests[] = [$name, true, null];
    } catch (Throwable $e) {
        $failures++;
        echo "  [FAIL] {$name}: {$e->getMessage()} @ {$e->getFile()}:{$e->getLine()}\n";
        $tests[] = [$name, false, $e->getMessage()];
    }
}

function assertSame2($expected, $actual, string $message): void
{
    if ($expected !== $actual) {
        throw new RuntimeException("{$message} (expected " . var_export($expected, true) . ', got ' . var_export($actual, true) . ')');
    }
}

function assertTrue2(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

// ---------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------

function seedFullyRegisteredUser(PDO $db, bool $sqlite): array
{
    if ($sqlite) {
        $db->exec('DELETE FROM users');
        $db->exec('DELETE FROM user_emails');
        $db->exec('DELETE FROM auth_sessions');
        $db->exec('DELETE FROM authorization_transactions');

        $db->exec('INSERT INTO users (public_id, registration_status, profile_completed_at) VALUES ("usr_test", "complete", "2024-01-01 00:00:00")');
        $userId = (int) $db->lastInsertId();
        $db->exec('INSERT INTO user_emails (user_id, email, normalized_email, is_primary, verified_at) VALUES (' . $userId . ', "approver@example.com", "approver@example.com", 1, "2024-01-01 00:00:00")');
        return ['id' => $userId];
    }

    $db->exec('DELETE FROM authorization_transactions');
    $db->exec('DELETE FROM auth_sessions');
    $db->exec('DELETE FROM user_emails');
    $db->exec('DELETE FROM users');

    $identity = new IdentityService($db);
    $user = $identity->findOrCreateVerifiedUser('approver@example.com');
    $identity->completeProfile((int) $user['id'], 'Test', 'User', '+15550001111');
    return ['id' => (int) $user['id']];
}

// ---------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------

$pdo = makeDatabase($useSqlite);
$svc = new AuthorizationService($pdo);
$user = seedFullyRegisteredUser($pdo, $useSqlite);
$approverToken = null;

echo "Running device authorization tests (mode: " . ($useSqlite ? 'sqlite' : 'mysql') . ")\n";

test('create returns full contract', function () use ($svc) {
    $r = $svc->create('cli', ['device_name' => 'Workstation', 'platform' => 'Windows 11', 'cli_version' => '0.1.0'], '127.0.0.1');
    foreach (['status', 'reference', 'browser_url', 'poll_token', 'expires_in', 'interval'] as $k) {
        assertTrue2(array_key_exists($k, $r), "missing key {$k}");
    }
    assertSame2('pending', $r['status'], 'status');
    assertTrue2(str_starts_with($r['reference'], 'ref_'), 'reference prefix');
    assertTrue2(str_starts_with($r['poll_token'], 'poll_sec_'), 'poll token prefix');
    assertTrue2(str_contains($r['browser_url'], '/sign-in/cli/identity?id=' . $r['reference']), 'browser url');
    assertTrue2($r['expires_in'] >= 60, 'expires_in');
    assertTrue2($r['interval'] >= 2, 'interval');
});

test('get returns browser-safe projection without poll credential', function () use ($svc) {
    $created = $svc->create('cli', ['device_name' => 'W2', 'platform' => 'Linux'], '10.0.0.1');
    $res = $svc->get($created['reference']);
    assertTrue2($res['found'], 'found');
    $t = $res['transaction'];
    foreach (['reference', 'status', 'device_name', 'platform', 'ip_address', 'scope', 'requested_at', 'expires_at'] as $k) {
        assertTrue2(array_key_exists($k, $t), "missing browser field {$k}");
    }
    foreach (['poll_token', 'poll_token_hash', 'poll_token_lookup', 'user_id', 'credential_issued', 'issued_token_hash'] as $k) {
        assertTrue2(!array_key_exists($k, $t), "browser projection must not leak {$k}");
    }
});

test('poll rejects invalid poll token', function () use ($svc) {
    $r = $svc->poll('poll_sec_invalid');
    assertSame2(false, $r['ok'], 'ok');
    assertSame2(401, $r['code'], 'code');
});

test('approve requires authentication', function () use ($svc) {
    $created = $svc->create('cli', ['device_name' => 'W3', 'platform' => 'macOS'], '127.0.0.2');
    $r = $svc->approve($created['reference'], '', 'web');
    assertSame2(false, $r['ok'], 'ok');
    assertSame2(401, $r['code'], 'code');
});

test('approve rejects invalid session token', function () use ($svc) {
    $created = $svc->create('cli', ['device_name' => 'W4', 'platform' => 'macOS'], '127.0.0.2');
    $r = $svc->approve($created['reference'], str_repeat('a', 64), 'web');
    assertSame2(false, $r['ok'], 'ok');
    assertSame2(401, $r['code'], 'code');
});

test('approve rejects profile_completion (non-full scope) sessions', function () use ($pdo, $svc, $useSqlite) {
    global $user;
    // Create a profile_completion session manually via production SessionService:
    // make a fresh pending_profile user first.
    if ($useSqlite) {
        $pdo->exec('INSERT INTO users (public_id, registration_status) VALUES ("usr_pending", "pending_profile")');
        $pendingId = (int) $pdo->lastInsertId();
        $pdo->exec('INSERT INTO user_emails (user_id, email, normalized_email, is_primary, verified_at) VALUES (' . $pendingId . ', "pending@example.com", "pending@example.com", 1, "2024-01-01 00:00:00")');
    } else {
        $identity = new IdentityService($pdo);
        $u = $identity->findOrCreateVerifiedUser('pending@example.com');
        $pendingId = (int) $u['id'];
    }

    $sessions = new SessionService($pdo);
    $session = $sessions->create($pendingId);
    assertSame2('profile_completion', $session['scope'], 'expected profile_completion scope');

    $created = $svc->create('cli', ['device_name' => 'W5', 'platform' => 'iOS'], '127.0.0.3');
    $r = $svc->approve($created['reference'], $session['token'], 'web');
    assertSame2(false, $r['ok'], 'ok');
    assertSame2(403, $r['code'], 'code');
});

test('full approve -> poll -> redeem flow delivers production session once', function () use ($pdo, $svc, $user, &$approverToken) {
    $sessions = new SessionService($pdo);
    $approver = $sessions->create($user['id']);
    $approverToken = $approver['token'];
    assertSame2('full', $approver['scope'], 'approver scope');

    $created = $svc->create('cli', ['device_name' => 'W6', 'platform' => 'Windows 11', 'cli_version' => '0.1.0'], '127.0.0.4');

    // Initial poll: pending
    $p = $svc->poll($created['poll_token']);
    assertSame2(true, $p['ok'], 'ok');
    assertSame2('pending', $p['status'], 'status');

    // Approve with the production bearer token
    $a = $svc->approve($created['reference'], $approver['token'], 'web');
    assertSame2(true, $a['ok'], 'approve ok');
    assertSame2('approved', $a['status'], 'approve status');

    // Poll again: approved + redeem hint, but no credentials in poll
    $p2 = $svc->poll($created['poll_token']);
    assertSame2('approved', $p2['status'], 'poll status');
    assertSame2(true, $p2['redeem'], 'redeem hint');
    assertTrue2(!isset($p2['token']), 'poll must not deliver token');

    // Redeem once: production session token
    $r = $svc->redeem($created['poll_token']);
    assertSame2(true, $r['ok'], 'redeem ok');
    assertSame2('approved', $r['status'], 'redeem status');
    assertTrue2(!empty($r['token']), 'token present');
    assertTrue2(preg_match('/^[a-f0-9]{64}$/', $r['token']) === 1, 'token is 64-hex production format');
    assertTrue2(!empty($r['expires_at']), 'expires_at present');
    assertTrue2(isset($r['user']['public_id']), 'user public_id present');

    // Verify the issued token validates against the production SessionService
    $validated = $sessions->validate($r['token']);
    assertTrue2(is_array($validated), 'issued token must validate via production SessionService');
    assertSame2($user['id'], $validated['user_id'], 'session belongs to approver');
    assertSame2('full', $validated['scope'], 'session scope full');

    // Second redeem attempt: one-time guard
    $r2 = $svc->redeem($created['poll_token']);
    assertSame2(false, $r2['ok'], 'second redeem ok');
    assertSame2(409, $r2['code'], 'second redeem code');
});

test('deny flow blocks credential delivery', function () use ($svc, $approverToken) {
    $created = $svc->create('cli', ['device_name' => 'W7', 'platform' => 'Windows 10'], '127.0.0.5');
    $d = $svc->deny($created['reference'], $approverToken, 'web');
    assertSame2(true, $d['ok'], 'deny ok');
    assertSame2('denied', $d['status'], 'deny status');

    $p = $svc->poll($created['poll_token']);
    assertSame2('denied', $p['status'], 'poll status');

    $r = $svc->redeem($created['poll_token']);
    assertSame2(false, $r['ok'], 'redeem ok');
    assertSame2(403, $r['code'], 'redeem code');
});

test('decision client type header is required but not compared to transaction client type', function () use ($svc, $approverToken) {
    $created = $svc->create('cli', ['device_name' => 'W8', 'platform' => 'Linux'], '127.0.0.6');
    // Approving via 'web' (the Identity app) for a 'cli' transaction is legitimate.
    $r = $svc->approve($created['reference'], $approverToken, 'web');
    assertSame2(true, $r['ok'], 'approve from web for cli transaction must succeed');
});

test('expired authorization cannot be approved or redeemed', function () use ($pdo, $svc, $approverToken) {
    $created = $svc->create('cli', ['device_name' => 'W9', 'platform' => 'Windows 11'], '127.0.0.7');

    // Force expiry in the past.
    $stmt = $pdo->prepare('UPDATE authorization_transactions SET expires_at = ? WHERE reference = ?');
    $stmt->execute(['2020-01-01 00:00:00', $created['reference']]);

    $a = $svc->approve($created['reference'], $approverToken, 'web');
    assertSame2(false, $a['ok'], 'approve ok');
    assertSame2(410, $a['code'], 'approve code');

    $p = $svc->poll($created['poll_token']);
    assertSame2('expired', $p['status'], 'poll status');

    $r = $svc->redeem($created['poll_token']);
    assertSame2(false, $r['ok'], 'redeem ok');
    assertSame2(403, $r['code'], 'redeem code');
});

test('double approve is rejected (409)', function () use ($svc, $approverToken) {
    $created = $svc->create('cli', ['device_name' => 'W10', 'platform' => 'Linux'], '127.0.0.8');
    $a1 = $svc->approve($created['reference'], $approverToken, 'web');
    assertSame2(true, $a1['ok'], 'first approve');
    $a2 = $svc->approve($created['reference'], $approverToken, 'web');
    assertSame2(false, $a2['ok'], 'second approve');
    assertSame2(409, $a2['code'], 'second approve code');
});

test('second redeem before guard cannot issue duplicate session', function () use ($pdo, $svc, $approverToken) {
    // Simulate a race: two rows both holding valid poll tokens is impossible
    // (unique lookup index), so instead verify the guard directly.
    $created = $svc->create('cli', ['device_name' => 'W11', 'platform' => 'Linux'], '127.0.0.9');
    $svc->approve($created['reference'], $approverToken, 'web');

    // Manually mark issued to emulate concurrent redemption, then redeem again.
    $stmt = $pdo->prepare('UPDATE authorization_transactions SET credential_issued = 1 WHERE reference = ?');
    $stmt->execute([$created['reference']]);

    $r = $svc->redeem($created['poll_token']);
    assertSame2(false, $r['ok'], 'redeem ok');
    assertSame2(409, $r['code'], 'redeem code');
});

echo "\nDone. " . (count($tests) - $failures) . '/' . count($tests) . " tests passed.\n";
exit($failures === 0 ? 0 : 1);
