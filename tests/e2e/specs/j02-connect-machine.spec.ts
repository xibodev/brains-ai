import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J2 — Connect a machine.
 *
 * Lifecycle: withdrawn. This spec proves containment only.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J2 withdrawn Runtime routes are not discoverable and fail closed', async ({ page, consoleGuard }) => {
  await page.goto('/app/command-center');
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /connect a machine/i })).toHaveCount(0);
  await page.waitForLoadState('networkidle').catch(() => {});

  for (const route of [
    '/app/runtimes',
    '/app/runtimes/ci-machine',
    '/app/labs/runtimes',
    '/app/labs/runtimes/ci-machine',
  ]) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }

  consoleGuard.assertClean();
});
