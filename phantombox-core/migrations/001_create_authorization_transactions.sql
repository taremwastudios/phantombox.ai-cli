-- PhantomBox Core - Migration: shared authorization transactions
-- Phantom Illusions Studio
--
-- One persistent transaction table shared by ALL client types (web, cli, app).
-- Created because no existing table stores cross-client authorization
-- transactions; users/sessions/email OTP tables are untouched.

CREATE TABLE IF NOT EXISTS `authorization_transactions` (
    `id`                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- Public transaction identifier (unique, non-guessable)
    `reference`           VARCHAR(64)  NOT NULL,

    -- Established client type header value: web | cli | app
    `client_type`         VARCHAR(16)  NOT NULL,

    -- Device/client details shown to the approving user in the browser
    `device_name`         VARCHAR(255) NOT NULL,
    `device_platform`     VARCHAR(255) NOT NULL,
    `device_version`      VARCHAR(64)  NOT NULL DEFAULT '',
    `ip_address`          VARCHAR(45)  NOT NULL DEFAULT '',

    -- Polling credential: ONLY a hash is persisted. The plaintext value is
    -- shown once to the client at creation and never stored.
    `poll_token_hash`     VARCHAR(255) NOT NULL,
    `poll_token_lookup`   CHAR(64)     NOT NULL COMMENT 'SHA-256 lookup index, not the credential',

    -- Requested permissions / scope
    `scope`               VARCHAR(255) NOT NULL,

    -- pending | approved | denied | expired
    `status`              VARCHAR(16)  NOT NULL DEFAULT 'pending',

    -- Approving authenticated user (NULL until approved)
    `user_id`             INT UNSIGNED NULL,

    -- Secure credential delivery state (one-time redemption guard)
    `credential_issued`   TINYINT(1)   NOT NULL DEFAULT 0,
    `credential_issued_at` DATETIME    NULL,

    -- Timestamps
    `created_at`          DATETIME     NOT NULL,
    `decided_at`          DATETIME     NULL,
    `expires_at`          DATETIME     NOT NULL,

    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_authorization_reference` (`reference`),
    UNIQUE KEY `uq_authorization_poll_lookup` (`poll_token_lookup`),
    KEY `idx_authorization_status_expiry` (`status`, `expires_at`),
    KEY `idx_authorization_client_type` (`client_type`),
    CONSTRAINT `fk_authorization_user` FOREIGN KEY (`user_id`)
        REFERENCES `users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
