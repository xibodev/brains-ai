import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J9 — Configure Brains and GitHub linkage.
 *
 * Advertised browser evidence focuses on Operations config/access surfaces.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J9.1 supported local configuration is truthful and contained', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/config/local');
  await expect(page.getByRole('heading', { name: 'Configure' })).toBeVisible();
  await expect(page.getByText('Supported local configuration')).toBeVisible();
  await expect(page.getByText('service.authentication')).toBeVisible();
  await expect(page.getByLabel('service.rate_limit_per_minute')).toBeVisible();
  await expect(page.getByLabel('sqlite.busy_timeout_ms')).toBeVisible();
  await expect(page.getByLabel('sqlite.enforce_foreign_keys')).toBeVisible();
  await expect(page.getByText(/secret values and filesystem locations are omitted/i)).toBeVisible();
  await expect(page.getByText(/provider|smtp|gateway preamble|bridge/i)).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  consoleGuard.assertClean();
});

test('J9.2 supported write reports a live-reload outcome', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/config/local');
  const rateLimit = page.getByLabel('service.rate_limit_per_minute');
  const original = Number(await rateLimit.inputValue());
  await rateLimit.fill(String(original === 100000 ? original - 1 : original + 1));
  await page.getByRole('button', { name: 'Save supported changes' }).click();
  await expect(page.getByText('reloaded', { exact: true })).toBeVisible();
  consoleGuard.assertClean();
});
