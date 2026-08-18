import { test, expect, signIn } from '../fixtures/console.js';
import { seedWorkspace } from '../fixtures/seed.js';

/**
 * J5 — Create a Project, link a Workspace, and honor deep links.
 *
 * Authority: F4, AC-F4-01, AC-F0-05, AC-B2-02, and AC-B5-01.
 */

test.beforeAll(() => {
  seedWorkspace();
});

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J5.1 project creation persists the selected workspace', async ({ page }) => {
  await page.goto('/app/projects');
  await page.getByRole('button', { name: /new project/i }).first().click();

  const name = `Workspace project ${Date.now()}`;
  await page.getByLabel(/^name$/i).fill(name);
  await page.locator('select').first().selectOption({ index: 1 });
  const [response] = await Promise.all([
    page.waitForResponse((resp) => resp.url().includes('/v1/orgs/demo/projects')),
    page.getByRole('button', { name: /^create$/i }).click(),
  ]);
  expect(response.ok(), `project create failed: ${response.status()}`).toBeTruthy();
  const project = await response.json();

  await expect(page.getByText(name).first()).toBeVisible();
  await page.goto(`/app/projects/${project.code}`);
  await expect(page.getByRole('heading', { name })).toBeVisible();
  await expect(page.locator('[data-testid="project-workspace"]')).toContainText('E2E Workspace');
});

test('J5.2 an unknown project deep link says not found', async ({ page, consoleGuard }) => {
  await page.goto('/app/projects/PRJ-NOT-REAL');
  await expect(page.locator('[data-testid="project-not-found"]')).toContainText('PRJ-NOT-REAL');
  consoleGuard.assertClean();
});
