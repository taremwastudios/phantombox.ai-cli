<?php
/**
 * PhantomBox Core - Identity page controller (HTML views)
 *
 * Serves the browser-facing Identity pages. This controller only READS
 * through the existing services; it performs no authentication of its own
 * and never mutates users, sessions or authorization transactions.
 *
 *   GET /sign-in                        -> src/Views/sign-in.php
 *   GET /sign-in/cli/identity?id=REF    -> src/Views/sign-in-cli-identity.php
 *
 * The JSON API under /v1/identity/* is untouched and remains the only
 * place where authentication actually happens.
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

namespace PhantomBox\Core\Controllers;

use PhantomBox\Core\Services\AuthorizationService;
use Throwable;

class IdentityPageController
{
    private const VIEWS = __DIR__ . '/../Views';

    public function signIn(): void
    {
        $this->render('Sign in | PhantomBox Identity', 'sign-in.php', [
            'initialEmail' => self::queryString('email'),
        ]);
    }

    /**
     * CLI device authorization page.
     *
     * Reads the transaction through the existing AuthorizationService::get()
     * so the page shows exactly what the Core knows about the request.
     */
    public function deviceAuthorization(): void
    {
        $reference = trim(self::queryString('id'));
        $lookupError = null;
        $transaction = null;

        if ($reference === '' || preg_match('/^[a-zA-Z0-9_\-\.:]{6,128}$/', $reference) !== 1) {
            $lookupError = 'This authorization link is missing a valid reference. '
                . 'Open the link printed by the CLI command.';
        } else {
            try {
                $result = (new AuthorizationService())->get($reference);
                if (!$result['found']) {
                    $lookupError = 'Authorization reference not found. It may have been removed. '
                        . 'Run the CLI sign-in command again.';
                } else {
                    $transaction = $result['transaction'];
                }
            } catch (Throwable $e) {
                error_log('PhantomBox Identity page: authorization lookup failed.');
                $lookupError = 'The Identity service is temporarily unavailable. Please try again shortly.';
            }
        }

        // The browser keeps its bearer token in sessionStorage, so the page
        // cannot know the sign-in state server-side. The client script
        // redirects to /sign-in when no token is present.
        $this->render('Authorize device | PhantomBox Identity', 'sign-in-cli-identity.php', [
            'reference'   => $reference,
            'lookupError' => $lookupError,
            'transaction' => $transaction,
        ]);
    }

    // -------------------------------------------------------------------

    private static function queryString(string $key): string
    {
        $value = $_GET[$key] ?? '';
        return is_string($value) ? $value : '';
    }

    /**
     * Render a panel template inside the shared two-panel layout.
     */
    private function render(string $pageTitle, string $panel, array $data = []): void
    {
        $viewFile = self::VIEWS . '/' . $panel;
        if (!is_file($viewFile)) {
            http_response_code(500);
            header('Content-Type: text/plain; charset=UTF-8');
            echo 'Identity view unavailable.';
            return;
        }

        header('Content-Type: text/html; charset=UTF-8');
        header('X-Frame-Options: DENY');
        header('Referrer-Policy: no-referrer');

        // CSRF token placeholder: the JSON API authenticates with the bearer
        // session and does not require this value, but it is exposed so any
        // cookie-based CSRF layer added later works without template changes.
        $csrfToken = bin2hex(random_bytes(16));

        $viewData = $data;

        require self::VIEWS . '/layout.php';
    }
}
