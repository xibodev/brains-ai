import { test, expect, signIn } from '../fixtures/console.js';

const CORE_ROUTES = [
  '/app/command-center',
  '/app/workspaces',
  '/app/coordination',
  '/app/governance',
  '/app/operations',
  '/app/operations/config/local',
  '/app/operations/config/mcp',
  '/app/operations/config/health',
  '/app/act',
];

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

for (const viewport of [
  { name: 'desktop', width: 1366, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`J11 core routes expose a typed successful state without overflow at ${viewport.name} width`, async ({ page, consoleGuard }) => {
    await page.setViewportSize(viewport);
    for (const route of CORE_ROUTES) {
      await page.goto(route);
      await expect(page.locator('[data-async-state="success"]').first()).toBeAttached();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      expect(overflow, `${route} overflowed at ${viewport.width}px`).toBe(false);
    }
    consoleGuard.assertClean();
  });
}

test('J11 unknown routes and configuration sections stay put and disclose no requested path', async ({ page }) => {
  const privateLookingPath = '/app/private-looking/resource-123';
  await page.goto(privateLookingPath);
  await expect(page).toHaveURL(privateLookingPath);
  await expect(page.getByTestId('not-found')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible();
  await expect(page.getByText(privateLookingPath)).toHaveCount(0);

  await page.goto('/app/operations/config/not-a-section');
  await expect(page).toHaveURL('/app/operations/config/not-a-section');
  await expect(page.getByRole('heading', { name: 'Configuration section not found' })).toBeVisible();
});

test('J11 route state distinguishes loading, empty, service error, and unauthorized', async ({ page }) => {
  let releaseOverview!: () => void;
  const held = new Promise<void>((resolve) => { releaseOverview = resolve; });
  await page.route('**/v1/operator/overview', async (route) => {
    await held;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Temporarily unavailable' }) });
  });
  await page.goto('/app/command-center');
  await expect(page.locator('[data-async-state="loading"]')).toBeVisible();
  releaseOverview();
  await expect(page.locator('[data-async-state="error"]')).toBeVisible();

  await page.unroute('**/v1/operator/overview');
  await page.route('**/v1/operator/workspaces', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data: [] }),
  }));
  await page.goto('/app/workspaces');
  await expect(page.locator('[data-async-state="empty"]')).toBeVisible();

  await page.route('**/v1/operator/overview', (route) => route.fulfill({
    status: 403,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'Operator authorization required' }),
  }));
  await page.goto('/app/command-center');
  await expect(page.locator('[data-async-state="unauthorized"]')).toBeVisible();
  await expect(page.getByText('Authorization required')).toBeVisible();
});

test('J11 realtime loss is a visible degraded state while durable HTTP remains available', async ({ page }) => {
  await page.routeWebSocket('**/v1/ws', (socket) => {
    socket.close({ code: 1012, reason: 'synthetic restart' });
  });
  await page.goto('/app/command-center');
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  await expect(page.locator('[data-connection-state="degraded"]')).toContainText('Durable HTTP state remains available');
});

test('J11 command palette traps focus, closes with Escape, and restores its opener', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto('/app/command-center');
  const opener = page.getByRole('button', { name: /Search workspaces and actions/i });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Command palette' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('textbox', { name: 'Find a view or typed action' })).toBeFocused();

  await page.keyboard.press('Shift+Tab');
  await expect(dialog.getByRole('option').last()).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(dialog.getByRole('textbox')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});
