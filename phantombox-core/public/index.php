<?php
/**
 * PhantomBox Core - Front controller
 * Canonical hostname: https://core-phantombox.phantom-illusions-studio.com
 *
 * Phantom Illusions Studio
 */

declare(strict_types=1);

require __DIR__ . '/../src/autoload.php';

use PhantomBox\Core\Router;

(new Router())->dispatch();
