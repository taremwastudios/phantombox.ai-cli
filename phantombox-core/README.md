PhantomBox Core
===============

Canonical hostname: https://core-phantombox.phantom-illusions-studio.com

Sole authentication authority for PhantomBox (Web, CLI, Phone App).
Document root: `public/` (front controller: `public/index.php`).

Shared Authorization Lifecycle
-----------------------------
ONE transaction system for all client types. The client is identified by the
established request header:

    X-PhantomBox-Client: web | cli | app

Endpoints (all JSON):

| Method | Path                                        | Auth                        | Purpose |
|--------|---------------------------------------------|-----------------------------|---------|
| POST   | /v1/identity/cli/authorization              | X-PhantomBox-Client         | Create pending transaction |
| GET    | /v1/identity/cli/authorization/{reference}  | (browser-facing)            | Transaction details, NO poll credential |
| POST   | /v1/identity/cli/authorization/approve      | Bearer session + header     | Explicit approval |
| POST   | /v1/identity/cli/authorization/deny         | Bearer session + header     | Explicit denial |
| POST   | /v1/identity/cli/authorization/poll         | poll_token + header         | Status polling |
| POST   | /v1/identity/cli/authorization/redeem       | poll_token + header         | One-time credential delivery |
| POST   | /v1/identity/cli/authorization/expire       | (maintenance)               | Sweep expired pending transactions |

Create response contract (identical for web/cli/app):

    {
      "status": "pending",
      "reference": "ref_...",
      "browser_url": "https://phantomboxai.phantom-illusions-studio.com/sign-in/cli/identity?id=REFERENCE",
      "poll_token": "poll_sec_...",
      "expires_in": 600,
      "interval": 5
    }

Poll responses: `{"status":"pending"}`, `{"status":"denied"}`,
`{"status":"expired"}` (HTTP 410), or `{"status":"approved","redeem":true}`
followed by a one-time `redeem` call returning `{"status":"approved","token":...,"user":...,"expires_at":...}`.

Security invariants: approval requires a fully authenticated session
(profile_required sessions are rejected); the polling credential is stored
hashed and never sent to the browser; credentials are issued exactly once and
only on the approved state; decisions are atomic and final.

Migration
---------
Apply `migrations/001_create_authorization_transactions.sql` once per
environment. It only adds the `authorization_transactions` table; existing
`users`, `sessions` and OTP tables are not modified.

Local development
-----------------
    php -S 127.0.0.1:8600 -t public router.php
