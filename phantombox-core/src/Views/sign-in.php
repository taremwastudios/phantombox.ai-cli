<?php
/**
 * PhantomBox Identity - sign-in panel (email identification, OTP, profile)
 *
 * Three steps live in one page and are switched client-side in response to
 * what the Core API returns:
 *
 *   1. email   -> POST /v1/identity/email/request
 *                 the response carries `profile_required`, telling the UI
 *                 whether this is an existing account or a new registration
 *   2. code    -> POST /v1/identity/email/verify
 *   3. profile -> POST /v1/identity/profile/complete   (new accounts only)
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

/** @var array $initialEmail */
$initialEmail = (string) ($initialEmail ?? '');
?>
<header class="auth-heading">
    <span class="auth-kicker" id="authKicker">Sign in</span>
    <h1 class="auth-title" id="authTitle">Sign in to PhantomBox</h1>
    <p class="auth-subtitle" id="authSubtitle">Enter your email address to receive a secure one-time verification code.</p>
</header>

<nav class="steps" aria-label="Sign-in progress">
    <span class="step-item">
        <span class="step-dot active" id="stepDot1" aria-current="step">1</span>
        <span class="step-caption" id="stepCaption1">Email</span>
    </span>
    <span class="step-rule"></span>
    <span class="step-item">
        <span class="step-dot" id="stepDot2">2</span>
        <span class="step-caption hidden" id="stepCaption2">Verify</span>
    </span>
    <span class="step-rule"></span>
    <span class="step-item">
        <span class="step-dot" id="stepDot3">3</span>
        <span class="step-caption hidden" id="stepCaption3">Profile</span>
    </span>
</nav>

<div class="alert alert-error hidden" id="signInAlert" role="alert" aria-live="polite">
    <span id="signInAlertText"></span>
</div>

<!-- Step 1 — email identification -->
<form id="requestOtpForm" class="auth-form step-view" novalidate autocomplete="on">
    <div class="form-group">
        <label class="form-label" for="emailInput">Email address</label>
        <input
            class="form-input"
            type="email"
            id="emailInput"
            name="email"
            value="<?= h($initialEmail) ?>"
            placeholder="you@example.com"
            autocomplete="email"
            autocapitalize="none"
            spellcheck="false"
            required
            autofocus
        >
        <p class="form-hint">We use this address to identify your account and send a single-use code.</p>
    </div>

    <button class="btn btn-block" type="submit" id="btnRequestOtp">
        <span class="btn-text">Send verification code</span>
        <span class="btn-spinner hidden" aria-hidden="true"></span>
    </button>
</form>

<!-- Step 2 — one-time code -->
<form id="verifyOtpForm" class="auth-form step-view hidden" novalidate>
    <div class="email-confirm">
        <span>Code sent to <span class="email-value" id="displayEmailTarget"></span></span>
        <button class="btn-link" type="button" id="btnChangeEmail">Change</button>
    </div>

    <div class="form-group">
        <label class="form-label" for="codeInput">Verification code</label>
        <input
            class="form-input"
            type="text"
            id="codeInput"
            name="code"
            placeholder="••••••"
            maxlength="8"
            inputmode="numeric"
            autocomplete="one-time-code"
            spellcheck="false"
            required
        >
        <p class="form-hint">Six digits from your inbox. The code expires after ten minutes.</p>
    </div>

    <button class="btn btn-block" type="submit" id="btnVerifyOtp">
        <span class="btn-text">Verify code</span>
        <span class="btn-spinner hidden" aria-hidden="true"></span>
    </button>

    <div class="resend-row">
        <button class="btn-link" type="button" id="btnResendOtp">Resend code</button>
    </div>
</form>

<!-- Step 3 — profile completion (new accounts only) -->
<form id="completeProfileForm" class="auth-form step-view hidden" novalidate>
    <div class="form-row">
        <div class="form-group">
            <label class="form-label" for="firstNameInput">First name</label>
            <input class="form-input" type="text" id="firstNameInput" name="first_name"
                   autocomplete="given-name" required>
        </div>
        <div class="form-group">
            <label class="form-label" for="lastNameInput">Last name</label>
            <input class="form-input" type="text" id="lastNameInput" name="last_name"
                   autocomplete="family-name" required>
        </div>
    </div>

    <div class="form-group">
        <label class="form-label" for="phoneInput">Phone number</label>
        <input class="form-input" type="tel" id="phoneInput" name="phone_number"
               placeholder="+256700000000" autocomplete="tel" required>
        <p class="form-hint">Include your country code, e.g. +256…</p>
    </div>

    <button class="btn btn-block" type="submit" id="btnCompleteProfile">
        <span class="btn-text">Complete profile</span>
        <span class="btn-spinner hidden" aria-hidden="true"></span>
    </button>
</form>

<p class="auth-footnote">Secured by PhantomBox Core. Codes are single-use; sessions are issued by the Core API only.</p>
