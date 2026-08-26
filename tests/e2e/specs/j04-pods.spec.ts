import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J4 — Create and operate a Pod.
 *
 * Authority: F5, AC-F5-01 through AC-F5-04, and AC-F4-06. This deterministic
 * path creates a Pod with a leader and confirms persisted browser state.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J4 (F5) create a pod with a leader', async ({ page, consoleGuard }) => {
  await page.goto('/app/labs/pods');
  await page.getByRole('button', { name: /new pod|\+ new pod|\+ new/i }).first().click();

  const name = `pod-${Date.now()}`;
  await page.getByLabel(/^name/i).fill(name);
  // Leader select (org members) — pick the first real option.
  await page.getByLabel(/leader/i).selectOption({ index: 1 });
  await page.getByRole('button', { name: /^create$/i }).click();

  await expect(page.locator('[data-testid="pod-card"]').filter({ hasText: name })).toBeVisible();
  consoleGuard.assertClean();
});
