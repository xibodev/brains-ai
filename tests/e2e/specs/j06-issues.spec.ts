import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J6 — Create, assign, and dispatch an Issue.
 *
 * Lifecycle: withdrawn. This spec proves containment only.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J6 withdrawn Issue routes fail closed and no Issue activation controls are discoverable', async ({ page }) => {
  await page.goto('/app/coordination');
  await expect(page.getByRole('heading', { name: 'Coordination' })).toBeVisible();
  await expect(page.getByRole('button', { name: /dispatch/i })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);

  for (const route of ['/app/issues', '/app/issues/ISS-1', '/app/labs/issues']) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }
});
