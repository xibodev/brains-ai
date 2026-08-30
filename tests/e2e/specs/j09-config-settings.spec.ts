import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J9 — Configure Brains and GitHub linkage.
 *
 * Advertised browser evidence focuses on Operations config/access surfaces.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J9.1 operations config sections remain reachable without Labs activation', async ({ page }) => {
  await page.goto('/app/operations/config/general');
  await expect(page.getByRole('heading', { name: 'Configure' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Runtime overlay' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);

  await page.goto('/app/operations/config/integrations');
  await expect(page.getByRole('heading', { name: 'Configure' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Integrations' })).toBeVisible();
});

test('J9.2 operations access usage remains reachable', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/access/usage');
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  await expect(page.locator('[data-testid="usage-summary"]')).toBeVisible();
  consoleGuard.assertClean();
});
