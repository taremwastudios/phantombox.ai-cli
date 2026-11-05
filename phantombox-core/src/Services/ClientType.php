<?php
/**
 * PhantomBox Core - Client type resolution
 *
 * ONE shared authorization system for web, cli and app clients.
 * The client type is identified exclusively through the established
 * request header:
 *
 *     X-PhantomBox-Client: web | cli | app
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Services;

final class ClientType
{
    public const HEADER = 'X-PhantomBox-Client';

    public const WEB = 'web';
    public const CLI = 'cli';
    public const APP = 'app';

    public const ALL = [self::WEB, self::CLI, self::APP];

    /**
     * Resolve the client type from the request header.
     * Returns null when the header is missing or has an unsupported value.
     */
    public static function fromRequest(): ?string
    {
        $raw = $_SERVER['HTTP_X_PHANTOMBOX_CLIENT'] ?? '';
        return self::normalize($raw);
    }

    public static function normalize(mixed $raw): ?string
    {
        $value = strtolower(trim((string) $raw));
        if ($value === '' || !in_array($value, self::ALL, true)) {
            return null;
        }
        return $value;
    }

    public static function requireFromRequest(): string
    {
        $type = self::fromRequest();
        if ($type === null) {
            throw new ClientTypeException(
                'Missing or invalid client identification header. '
                . 'Expected ' . self::HEADER . ': ' . implode('|', self::ALL) . '.'
            );
        }
        return $type;
    }
}
