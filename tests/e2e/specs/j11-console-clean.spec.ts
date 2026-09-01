import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J11 — Cross-cutting trust, realtime, errors, accessibility, and hygiene.
 *
 * Authority: J11, AC-F0-01 through AC-F0-05, AC-F3-01/07, AC-F9-03,
 * AC-B8-01 through AC-B8-04, and AC-B9-01 through AC-B9-03. The deterministic
 * sweep covers core routes, console/network hygiene, and active-Org persistence.
 */

const ROUTES = [
  '/app',
  '/app/command-center',
  '/app/workspaces',
  '/app/coordination',
  '/app/governance',
  '/app/operations',
  '/app/act',
  '/app/operations/config/email',
  '/app/operations/access/org',
];

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

for (const route of ROUTES) {
  test(`J11 console-clean on ${route}`, async ({ page, consoleGuard }) => {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    // Deep-loading any route must never get stuck on "No org" / "Loading…".
    await expect(page.getByText(/^\s*(no org|loading…?)\s*$/i)).toHaveCount(0);
    consoleGuard.assertClean();
  });
}

test('J11 (F0.2) the canonical command center persists across a full reload', async ({ page, consoleGuard }) => {
  await page.goto('/app/command-center');
  await page.waitForLoadState('networkidle').catch(() => {});
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  await expect(page.locator('.dock')).toHaveCount(0);

  await page.reload();
  await page.waitForLoadState('networkidle').catch(() => {});
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  consoleGuard.assertClean();
});

test('J11 workspace-first shell is responsive and keeps Labs fail-closed', async ({ page, consoleGuard }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/app/command-center');
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  await expect(page.locator('.control-sidebar')).toBeHidden();
  await expect(page.locator('.control-mobile-nav')).toBeVisible();
  await expect(page.locator('.control-mobile-act')).toBeVisible();
  await expect(page.getByText('Reading durable state')).toHaveCount(0, { timeout: 30_000 });

  const capabilities = await page.request.get('/v1/operator/capabilities');
  expect(capabilities.ok()).toBeTruthy();
  const body = await capabilities.json() as {
    labs_enabled: boolean;
    data: Array<Record<string, unknown>>;
  };
  expect(body.labs_enabled).toBe(false);
  expect(body.data.every((row) => !('command' in row) && !('argv' in row))).toBe(true);

  await page.goto('/app/labs');
  await expect(page).toHaveURL(/\/app\/command-center$/);
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  consoleGuard.assertClean();
});

test('J11 Operations exposes durable-mail readiness without embedded usage analytics', async ({ page, consoleGuard }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/app/operations');
  await expect(page.getByText('Durable mail', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Welcome follow-through' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Mailbox outcomes' })).toHaveCount(0);
  const hasOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(hasOverflow).toBe(false);
  consoleGuard.assertClean();
});

test('J11 Act uses typed HTTP and leaves host actions disabled', async ({ page, consoleGuard }) => {
  const title = `Typed browser task ${Date.now()}`;
  await page.goto('/app/act?capability=task.create&workspace=not-visible');
  const sheet = page.locator('.operator-action-sheet');
  await expect(sheet.getByRole('heading', { name: 'Create task' })).toBeVisible();
  const workspace = await sheet.getByLabel('Workspace').inputValue();
  expect(workspace).not.toBe('not-visible');
  await sheet.getByLabel('Title').fill(title);
  const [created] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes(`/v1/operator/workspaces/${encodeURIComponent(workspace)}/tasks`) &&
      response.request().method() === 'POST',
    ),
    sheet.getByRole('button', { name: 'Create task', exact: true }).click(),
  ]);
  expect(created.ok(), `typed task create failed: ${created.status()}`).toBeTruthy();

  await page.goto('/app/coordination');
  await expect(page.getByText(title)).toBeVisible();

  await page.goto('/app/act?capability=service.restart');
  await expect(page.locator('.operator-action-sheet').getByRole('button', {
    name: 'HTTP adapter required',
  })).toBeDisabled();
  consoleGuard.assertClean();
});
