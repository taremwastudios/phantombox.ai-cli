<?php
/**
 * PhantomBox Identity - CLI device authorization panel
 *
 * Reached from the CLI via
 *   /sign-in/cli/identity?id=REFERENCE
 *
 * The transaction details are read from the existing browser-facing Core
 * endpoint (GET /v1/identity/cli/authorization/{reference}); the decision is
 * submitted to the existing approve/deny endpoints with the user's bearer
 * session. Nothing here creates or simulates an authorization.
 *
 * Expected variables:
 *   $reference        string  authorization reference from ?id=
 *   $lookupError      ?string error text when the transaction could not load
 *   $transaction      ?array  browser-safe projection from Core
 *   $isSignedIn       bool    a browser session was presented (best effort)
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

$reference = (string) ($reference ?? '');
$lookupError = $lookupError ?? null;
$transaction = $transaction ?? null;
$isSignedIn = (bool) ($isSignedIn ?? false);

$status = is_array($transaction) ? (string) ($transaction['status'] ?? 'pending') : '';
?>
<header class="auth-heading">
    <span class="auth-kicker">PhantomBox Identity</span>
    <h1 class="auth-title">Authorize this device</h1>
    <p class="auth-subtitle">
        A terminal workstation is requesting access to your PhantomBox account.
        Approve only if you started this sign-in yourself.
    </p>
</header>

<nav class="steps" aria-label="Authorization progress">
    <span class="step-dot done">1</span>
    <span class="step-rule"></span>
    <span class="step-dot done">2</span>
    <span class="step-rule"></span>
    <span class="step-dot active" aria-current="step">3</span>
    <span class="step-rule" style="flex:0 0 10px"></span>
    <span>Authorize</span>
</nav>

<div class="alert alert-error hidden" id="deviceAlert" role="alert" aria-live="polite">
    <span id="deviceAlertText"></span>
</div>

<?php if ($lookupError !== null): ?>
    <div class="alert alert-error" role="alert">
        <span><?= h($lookupError) ?></span>
    </div>
    <p class="auth-footnote">Run <strong>phantombox /user --signin</strong> again to start a fresh request.</p>

<?php elseif ($status !== 'pending'): ?>
    <?php
    // The transaction has already been resolved (or expired) on the Core side.
    $message = 'approved' === $status
        ? 'This authorization has already been approved.'
        : (('denied' === $status)
            ? 'This authorization request was denied.'
            : 'This authorization request has expired.');
    ?>
    <div class="completion-state <?= 'approved' === $status ? 'approved' : 'denied' ?>">
        <div class="completion-title"><?= h(ucfirst($status)) ?></div>
        <div class="completion-desc"><?= h($message) ?> You can close this tab.</div>
    </div>

<?php else: ?>
    <div class="device-summary" id="deviceSummary">
        <div class="row"><span class="k">Reference</span><span class="v"><?= h((string) $reference) ?></span></div>
        <div class="row"><span class="k">Workstation</span><span class="v"><?= h((string) ($transaction['device_name'] ?? 'Unknown device')) ?></span></div>
        <div class="row"><span class="k">Platform</span><span class="v"><?= h((string) ($transaction['platform'] ?? 'Unknown platform')) ?></span></div>
        <div class="row"><span class="k">Requested</span><span class="v"><?= h((string) ($transaction['requested_at'] ?? '')) ?></span></div>
        <div class="row"><span class="k">Expires</span><span class="v"><?= h((string) ($transaction['expires_at'] ?? '')) ?></span></div>
        <div class="row"><span class="k">Scope</span><span class="v"><?= h((string) ($transaction['scope'] ?? '')) ?></span></div>
        <div class="row"><span class="k">Request IP</span><span class="v"><?= h((string) ($transaction['ip_address'] ?? '')) ?></span></div>
    </div>

    <div class="device-actions" id="deviceActions">
        <button class="btn" type="button" id="btnApproveDevice"
                data-reference="<?= h((string) $reference) ?>"
                data-return-to="<?= h('/sign-in/cli/identity?id=' . $reference) ?>">
            <span class="btn-text">Approve device</span>
            <span class="btn-spinner hidden" aria-hidden="true"></span>
        </button>
        <button class="btn btn-ghost" type="button" id="btnDenyDevice"
                data-reference="<?= h((string) $reference) ?>"
                data-return-to="<?= h('/sign-in/cli/identity?id=' . $reference) ?>">
            <span class="btn-text">Deny</span>
            <span class="btn-spinner hidden" aria-hidden="true"></span>
        </button>
    </div>

    <div class="completion-state hidden" id="completionState">
        <div class="completion-title" id="completionTitle"></div>
        <div class="completion-desc" id="completionDesc"></div>
    </div>
<?php endif; ?>

<p class="auth-footnote">Approving issues a PhantomBox CLI session to the requesting workstation. You must be signed in to PhantomBox Identity in this browser.</p>
