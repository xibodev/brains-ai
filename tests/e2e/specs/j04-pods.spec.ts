import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J4 — Create and operate a Pod.
 *
 * Lifecycle: withdrawn. This spec proves containment only.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J4 withdrawn Pod routes fail closed and remain undiscoverable', async ({ page, consoleGuard }) => {
  await page.goto('/app/command-center');
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /new pod/i })).toHaveCount(0);
  await page.waitForLoadState('networkidle').catch(() => {});

  for (const route of [
    '/app/pods',
    '/app/pods/team-1',
    '/app/labs/pods',
    '/app/labs/pods/team-1',
  ]) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }

  consoleGuard.assertClean();
});
