import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J10 — Manage Org, members, usage, and reusable guidance.
 *
 * Lifecycle: F10 automation is withdrawn. This spec proves containment while
 * confirming advertised Access surfaces remain available.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J10 withdrawn automation routes fail closed', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/access/org');
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  await expect(page.getByText(/organisation/i)).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  await page.waitForLoadState('networkidle').catch(() => {});

  for (const route of ['/app/automation', '/app/labs/automation']) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }

  consoleGuard.assertClean();
});
