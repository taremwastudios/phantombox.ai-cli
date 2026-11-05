<?php
/**
 * PhantomBox Core - Database connection (shared PDO instance)
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Services;

use PDO;
use PDOException;
use PhantomBox\Core\Config\Config;
use RuntimeException;

class Database
{
    private static ?PDO $pdo = null;

    public static function connect(): PDO
    {
        if (self::$pdo instanceof PDO) {
            return self::$pdo;
        }

        $dsn = sprintf(
            'mysql:host=%s;port=%s;dbname=%s;charset=utf8mb4',
            Config::get('db_host'),
            Config::get('db_port'),
            Config::get('db_name')
        );

        try {
            self::$pdo = new PDO(
                $dsn,
                (string) Config::get('db_user'),
                (string) Config::get('db_pass'),
                [
                    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                    PDO::ATTR_EMULATE_PREPARES => false,
                ]
            );
        } catch (PDOException $e) {
            throw new RuntimeException('Database connection failed.', 0, $e);
        }

        return self::$pdo;
    }
}
