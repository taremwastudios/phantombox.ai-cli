<?php
/**
 * PhantomBox Identity - shared page layout
 *
 * Two-panel authentication shell: the left column is filled by each view's
 * "panel" template, the right column is the static branding/info panel.
 *
 * Expected variables:
 *   $pageTitle  string  document title
 *   $viewFile   string  absolute path of the panel template to render
 *   $viewData   array   variables for that template
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

if (!isset($pageTitle) || !isset($viewFile)) {
    throw new RuntimeException('Layout requires $pageTitle and $viewFile.');
}

$viewData = $viewData ?? [];
$csrfToken = (string) ($csrfToken ?? '');

/** Minimal escape helper (views only ever output text or attribute values). */
function h(?string $value): string
{
    return htmlspecialchars((string) $value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="csrf-token" content="<?= h($csrfToken) ?>">
    <meta name="robots" content="noindex, nofollow">
    <title><?= h($pageTitle) ?></title>

    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

    <link rel="icon" type="image/png" href="/assets/images/icon_light.png" media="(prefers-color-scheme: light)">
    <link rel="icon" type="image/png" href="/assets/images/icon_dark.png" media="(prefers-color-scheme: dark)">
    <link rel="stylesheet" href="/assets/css/identity.css">
</head>
<body>
<div class="identity-shell">

    <main class="auth-panel">
        <div class="auth-card">
            <?php
            // Panel template inherits the scope so it can use h() and $csrfToken.
            extract($viewData, EXTR_SKIP);
            require $viewFile;
            ?>
        </div>
    </main>

    <aside class="brand-panel">
        <figure class="brand-figure">
            <img class="brand-banner brand-banner-light" src="/assets/images/light.png" alt="PhantomBox">
            <img class="brand-banner brand-banner-dark" src="/assets/images/dark.png" alt="PhantomBox">
            <figcaption class="brand-tagline">Local-first AI runtime &middot; phantombox.ai</figcaption>
        </figure>

        <div class="info-block">
            <span class="info-title">Information</span>

            <div class="info-rows">
                <a class="info-row" href="https://phantom-illusions-studio.com" target="_blank" rel="noopener noreferrer">
                    <img src="/assets/images/studio.jpg" alt="Phantom Illusions Studio">
                    <span class="info-label">A Phantom Illusions Studio product</span>
                </a>
                <a class="info-row" href="https://github.com/taremwa" target="_blank" rel="noopener noreferrer">
                    <img src="/assets/images/taremwa.png" alt="Taremwa Andrew">
                    <span class="info-label">Built by Taremwa Andrew</span>
                </a>
            </div>

            <p class="info-note">
                One PhantomBox account signs you in to the CLI, the desktop app and
                every platform service. Codes are single-use and expire after ten minutes.
            </p>
        </div>
    </aside>

</div>

<script src="/assets/js/identity.js" defer></script>
</body>
</html>
