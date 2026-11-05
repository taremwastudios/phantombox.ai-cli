# PhantomBox Core — CLI Device Authorization Deployment

This feature adds the six device-authorization endpoints required by the
PhantomBox CLI, built entirely on top of the EXISTING production
authentication system (IdentityService, SessionService, EmailAuthService,
OTP services, rate limiting, mail transport, Database, and the production
`users` / `user_emails` / `auth_sessions` schema).

Nothing in the existing production authentication stack was modified.

## What gets uploaded

Upload these files into the production Core project root
(canonical host `core-phantombox.phantom-illusions-studio.com`):

    src/Authorization/AuthorizationService.php        (new)
    src/Authorization/AuthorizationController.php     (new)
    migrations/002_create_authorization_transactions.sql  (new, run manually)

And apply this one-file patch to the production front controller:

    public/index.php                                  (modified — see below)

The patch (in `public/index.php`):

1. Adds `require_once` lines for the new `src/Authorization/*` classes
   immediately after the existing `src/Database.php` require.
2. Inserts an authorization route block BEFORE the existing
   `if (!isset($routes[$path]))` 404 check:

       POST /v1/identity/cli/authorization           -> create()
       GET  /v1/identity/cli/authorization/{ref}     -> show()
       POST /v1/identity/cli/authorization/approve   -> approve()
       POST /v1/identity/cli/authorization/deny      -> deny()
       POST /v1/identity/cli/authorization/poll      -> poll()
       POST /v1/identity/cli/authorization/redeem    -> redeem()

All pre-existing routes (`/v1/identity/email/request`, `/verify`,
`/profile/complete`, `/session`, `/signout`, `/health`) are untouched and
continue to behave identically.

## Database migration (run manually — never automatic)

    mysql -u <user> -p phantombox_core < migrations/002_create_authorization_transactions.sql

The migration is `CREATE TABLE IF NOT EXISTS` and only adds the
`authorization_transactions` table. It does NOT alter `users`,
`user_emails`, `auth_sessions`, `email_otp_codes`, or any rate-limiting
table. There is no foreign key on `user_id` so the feature can never
cascade-delete or lock production user rows.

## Environment configuration (optional)

The service reads the same env vars already used by production plus:

    IDENTITY_APP_URL     base for browser_url (default: https://phantomboxai.phantom-illusions-studio.com)
    AUTHZ_TTL            authorization window seconds   (default 600, min 60)
    AUTHZ_POLL_INTERVAL  suggested poll interval        (default 5, min 2)

## Verification before go-live

1. `php -l` all uploaded files (all pass, PHP 8.3).
2. Endpoint tests (no deployment needed, uses SQLite):

       php phantombox-core/tests/authorization_endpoints_test.php --sqlite

   12/12 pass: create contract, browser-safe projection, invalid poll token,
   auth required, invalid session rejected, profile_completion rejected,
   full approve→poll→redeem delivers a production 64-hex auth_sessions
   token that validates via the production SessionService, deny blocks
   redemption, expiry (410), double-approve (409), one-time redemption
   guard (409).

   For a real MariaDB check, create a `phantombox_core_test` database,
   apply the migration, and run the same script without `--sqlite`
   (set TEST_DB_HOST/PORT/NAME/USER/PASS).

3. Smoke test with the real CLI:

       set PHANTOMBOX_CORE_API_URL=https://core-phantombox.phantom-illusions-studio.com
       phantombox /user --signin

## Security invariants preserved

- Approval requires a production bearer token whose `auth_sessions` row is
  unrevoked, unexpired, `session_scope = 'full'`, owned by an
  `account_status = 'active'` / `registration_status = 'complete'` user.
- Poll credentials are stored only as `password_hash` + a SHA-256 lookup
  index; the plaintext is shown once at creation.
- Issued tokens are stored only as SHA-256 (`issued_token_hash`); plaintext
  exists only inside `auth_sessions` via the production SessionService.
- Redemption is exactly-once (`credential_issued` guarded UPDATE); a lost
  race revokes the duplicate session it created.
- The browser-facing `GET {reference}` projection never exposes the poll
  credential, the issued token hash, user_id, or redemption state.
