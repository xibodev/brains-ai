import { test, expect, signIn } from '../fixtures/console.js';
import { seedWorkspace } from '../fixtures/seed.js';

/**
 * J7 — Dispatch and watch a Session.
 *
 * Advertised browser evidence here focuses on durable Workspace coordination.
 */

test.beforeAll(() => {
  seedWorkspace();
});

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J7.1 task creation in Act is durable across Coordination and Workspace views', async ({ page, consoleGuard }) => {
  const title = `J7 task ${Date.now()}`;
  await page.goto('/app/act?capability=task.create&workspace=e2e-workspace');
  const sheet = page.locator('.operator-action-sheet');
  await expect(sheet.getByRole('heading', { name: 'Create task' })).toBeVisible();
  await sheet.getByLabel('Title').fill(title);

  const [created] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes('/v1/operator/workspaces/e2e-workspace/tasks') &&
      response.request().method() === 'POST',
    ),
    sheet.getByRole('button', { name: 'Create task', exact: true }).click(),
  ]);
  expect(created.ok(), `typed task create failed: ${created.status()}`).toBeTruthy();

  await page.goto('/app/coordination');
  await expect(page.getByText(title)).toBeVisible();

  await page.goto('/app/workspaces/e2e-workspace');
  await page.getByRole('button', { name: /^work$/i }).click();
  await expect(page.getByText(title)).toBeVisible();
  consoleGuard.assertClean();
});

test('J7.2 handoff set in Act is visible in the Workspace communication tab', async ({ page, consoleGuard }) => {
  const handoffTitle = `J7 handoff ${Date.now()}`;
  await page.goto('/app/act?capability=handoff.set&workspace=e2e-workspace');
  const sheet = page.locator('.operator-action-sheet');
  await expect(sheet.getByRole('heading', { name: 'Set handoff' })).toBeVisible();
  await sheet.getByLabel('Title').fill(handoffTitle);
  await sheet.getByLabel('Description').fill('Carry forward durable coordination context.');

  const [saved] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes('/v1/operator/workspaces/e2e-workspace/handoffs') &&
      response.request().method() === 'POST',
    ),
    sheet.getByRole('button', { name: 'Set handoff', exact: true }).click(),
  ]);
  expect(saved.ok(), `handoff set failed: ${saved.status()}`).toBeTruthy();

  await page.goto('/app/workspaces/e2e-workspace');
  await page.getByRole('button', { name: /^communication$/i }).click();
  await expect(page.getByText(handoffTitle)).toBeVisible();
  consoleGuard.assertClean();
});
