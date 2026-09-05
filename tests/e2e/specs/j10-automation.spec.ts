import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J10 — Manage Org, members, usage, and reusable guidance.
 *
 * Lifecycle: F10 automation is withdrawn. This spec proves containment while
 * confirming withdrawn access and automation URLs remain unavailable.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J10 withdrawn automation routes fail closed', async ({ page, consoleGuard }) => {
  await page.goto('/app/operations/access/org');
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  await page.waitForLoadState('networkidle').catch(() => {});

  for (const route of ['/app/automation', '/app/labs/automation']) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(route);
    await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible();
  }

  consoleGuard.assertClean();
});
