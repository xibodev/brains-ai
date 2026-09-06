import { test, expect, signIn } from '../fixtures/console.js';
import { seedWorkspace } from '../fixtures/seed.js';

/**
 * J1 — Sign in and complete first run.
 *
 * Authority: F0/F6 and AC-F0-01/02 plus AC-F6-01 through AC-F6-05.
 * The normal install contract is Workspace-first and does not require Labs.
 */

test.beforeAll(() => {
  seedWorkspace();
});

test('J1.1 invalid credentials remain signed out and allow retry', async ({ page }) => {
  await page.goto('/app');
  const keyField = page.locator('input[type="password"], input[name="key"]').first();
  await expect(keyField).toBeVisible();
  await keyField.fill('definitely-wrong');
  await page.getByRole('button', { name: /sign in|continue|enter/i }).first().click();
  await expect(keyField).toBeVisible();
  await expect(page).toHaveURL(/admin\/login|\/app/);
});

test('J1.2 successful sign-in lands on Command Center and a Workspace control room', async ({ page, consoleGuard }) => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);

  await page.goto('/app/workspaces/e2e-workspace');
  await expect(page.getByRole('heading', { name: /E2E Workspace/i })).toBeVisible();
  await expect(page.getByRole('button', { name: /create task/i })).toBeVisible();
  consoleGuard.assertClean();
});

test('J1.3 withdrawn onboarding URLs fail closed without redirecting', async ({ page, consoleGuard }) => {
  await signIn(page);

  for (const route of ['/app/onboarding', '/app/labs/onboarding']) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(route);
    await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  }

  consoleGuard.assertClean();
});
