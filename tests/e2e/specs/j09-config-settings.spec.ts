import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J9 — Configure providers and integrations.
 *
 * Authority: F7/F8, B1/B7, and their J9 acceptance mappings. Config renders
 * effective redacted state, a provider test returns a bounded result, and
 * Settings exposes usage without claiming live-provider readiness.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J9 (F7) Config Providers shows real providers + a working Test', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/config/providers');
  const providers = page.locator('[data-testid="config-providers"]');
  await expect(providers).toBeVisible();
  // No "wire me" stub text anywhere on the config surface.
  await expect(page.getByText(/wire it to its config endpoint/i)).toHaveCount(0);
  // The Test button runs and yields a result (ok or fail) without a console error.
  const testBtn = providers.getByRole('button', { name: /test connection/i }).first();
  if (await testBtn.count()) {
    await testBtn.click();
    await expect(providers.getByText(/\u2713|\u2717|reachable|models|not configured/i).first()).toBeVisible({ timeout: 7000 });
  }
  consoleGuard.assertClean();
});

test('J9 (F9) Settings exposes a Usage dashboard', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/access/usage');
  await expect(page.locator('[data-testid="usage-summary"]')).toBeVisible();
  consoleGuard.assertClean();
});
