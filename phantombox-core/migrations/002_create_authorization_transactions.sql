-- PhantomBox Core - Migration 002
-- Authorization transactions for the shared (web | cli | app) device
-- authorization lifecycle, built on the production authentication schema.
--
-- This migration ONLY ADDS one new table.
-- It does NOT alter users, user_emails, auth_sessions, email_otp_codes
-- or any OTP rate-limiting table.
--
-- Apply manually per environment:
--   mysql -u <user> -p phantombox_core < migrations/002_create_authorization_transactions.sql

CREATE TABLE IF NOT EXISTS `authorization_transactions` (
    `id`                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- Public transaction identifier (unique, non-guessable)
    `reference`            VARCHAR(64)  NOT NULL,

    -- Established client type header value: web | cli | app
    `client_type`          VARCHAR(16)  NOT NULL,

    -- Device/client details shown to the approving user in the browser
    `device_name`          VARCHAR(255) NOT NULL,
    `device_platform`      VARCHAR(255) NOT NULL,
    `device_version`       VARCHAR(64)  NOT NULL DEFAULT '',
    `ip_address`           VARCHAR(45)  NOT NULL DEFAULT '',

    -- Polling credential: ONLY a hash is persisted. The plaintext value is
    -- returned once at creation and never stored.
    `poll_token_hash`      VARCHAR(255) NOT NULL,
    `poll_token_lookup`    CHAR(64)     NOT NULL COMMENT 'SHA-256 lookup index, not the credential',

    -- Requested permissions / scope
    `scope`                VARCHAR(255) NOT NULL,

    -- pending | approved | denied | expired
    `status`               VARCHAR(16)  NOT NULL DEFAULT 'pending',

    -- Approving user (production users.id), NULL until approved.
    -- NO foreign key is declared so this feature can never cascade-delete
    -- or lock production user rows.
    `user_id`              INT UNSIGNED NULL,

    -- One-time redemption guard + audit trail (token stored as SHA-256 only;
    -- the plaintext production session token lives solely in auth_sessions).
    `credential_issued`    TINYINT(1)   NOT NULL DEFAULT 0,
    `credential_issued_at` DATETIME     NULL,
    `issued_token_hash`    CHAR(64)     NULL,

    -- Timestamps (UTC, matching production UTC_TIMESTAMP() convention)
    `created_at`           DATETIME     NOT NULL,
    `decided_at`           DATETIME     NULL,
    `expires_at`           DATETIME     NOT NULL,

    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_authorization_reference` (`reference`),
    UNIQUE KEY `uq_authorization_poll_lookup` (`poll_token_lookup`),
    KEY `idx_authorization_status_expiry` (`status`, `expires_at`),
    KEY `idx_authorization_client_type` (`client_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
