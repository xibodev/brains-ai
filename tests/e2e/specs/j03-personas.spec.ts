import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J3 — Create and bind a Persona.
 *
 * Lifecycle: withdrawn. This spec proves containment only.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J3 withdrawn Persona routes fail closed and no enable switch is offered', async ({ page }) => {
  await page.goto('/app/command-center');
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /new persona|spawn/i })).toHaveCount(0);

  for (const route of ['/app/personas', '/app/personas/mason', '/app/labs/personas']) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }
});
