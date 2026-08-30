import { test, expect, signIn } from '../fixtures/console.js';
import { seedWorkspace } from '../fixtures/seed.js';

/**
 * J5 — Create a Project and link a Workspace.
 *
 * Lifecycle: Project actions are withdrawn; Workspace control-room navigation is advertised.
 */

test.beforeAll(() => {
  seedWorkspace();
});

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J5 withdrawn Project routes fail closed while Workspace route remains supported', async ({ page }) => {
  await page.goto('/app/workspaces/e2e-workspace');
  await expect(page.getByRole('heading', { name: /E2E Workspace/i })).toBeVisible();
  await expect(page.getByRole('button', { name: /create task/i })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);

  for (const route of ['/app/projects', '/app/projects/PRJ-1', '/app/labs/projects']) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }
});
