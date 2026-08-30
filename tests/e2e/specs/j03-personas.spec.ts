import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J3 — Create and bind a Persona.
 *
 * Lifecycle: withdrawn. This spec proves containment only.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J3 withdrawn Persona routes fail closed and no enable switch is offered', async ({ page, consoleGuard }) => {
  await page.goto('/app/command-center');
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /new persona|spawn/i })).toHaveCount(0);
  await page.waitForLoadState('networkidle').catch(() => {});

  for (const route of [
    '/app/personas',
    '/app/personas/mason',
    '/app/labs/personas',
    '/app/labs/personas/mason',
  ]) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }

  consoleGuard.assertClean();
});
