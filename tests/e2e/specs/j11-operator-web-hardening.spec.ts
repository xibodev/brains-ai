import { test, expect, signIn } from '../fixtures/console.js';

const CORE_ROUTES = [
  { path: '/app', heading: 'Command Center', content: 'Where work stands now' },
  { path: '/app/command-center', heading: 'Command Center', content: 'Where work stands now' },
  { path: '/app/workspaces', heading: 'Workspaces', content: 'Visible portfolio' },
  { path: '/app/workspaces/e2e-workspace', heading: 'Workspaces', content: 'Workspace control room' },
  { path: '/app/coordination', heading: 'Coordination', content: 'Work moving across the brain' },
  { path: '/app/governance', heading: 'Governance', content: 'Recent effects' },
  { path: '/app/operations', heading: 'Operations', content: 'Dependencies' },
  { path: '/app/operations/config/local', heading: 'Configure', content: 'Effective settings' },
  { path: '/app/operations/config/mcp', heading: 'Configure', content: 'Agent connections' },
  { path: '/app/operations/config/health', heading: 'Configure', content: 'Readiness, queues & recovery' },
  { path: '/app/act', heading: 'Act', content: 'Operator jobs' },
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
      await page.goto(route.path);
      await expect(page.getByRole('heading', { name: route.heading, exact: true })).toBeVisible();
      await expect(page.getByText(route.content, { exact: false }).first()).toBeVisible();
      await expect(page.locator('[data-async-state="loading"], [data-async-state="error"], [data-async-state="unauthorized"], [data-async-state="not_found"]')).toHaveCount(0);
      await expect(page.locator('[data-async-state="success"]')).not.toHaveCount(0);
      const containers = page.locator('.control-main, .operator-page, .operator-card, .operator-action-sheet, .masterdetail');
      for (let index = 0; index < await containers.count(); index += 1) {
        const box = await containers.nth(index).boundingBox();
        if (!box) continue;
        expect(box.width, `${route.path} container ${index} exceeded the viewport`).toBeLessThanOrEqual(viewport.width + 1);
        expect(box.x, `${route.path} container ${index} started outside the viewport`).toBeGreaterThanOrEqual(-1);
        expect(box.x + box.width, `${route.path} container ${index} ended outside the viewport`).toBeLessThanOrEqual(viewport.width + 1);
      }
      const controls = page.locator('main button:visible, main input:visible, main select:visible, main textarea:visible');
      for (let index = 0; index < await controls.count(); index += 1) {
        const control = controls.nth(index);
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        if (!box) continue;
        expect(box.width, `${route.path} control ${index} is wider than the viewport`).toBeLessThanOrEqual(viewport.width + 1);
        expect(box.x + box.width, `${route.path} control ${index} cannot be reached horizontally`).toBeLessThanOrEqual(viewport.width + 1);
      }
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

test('J11 unknown or scope-hidden Workspace deep links stay generic and never select a fallback', async ({ page }) => {
  for (const [slug, status] of [['unknown-workspace', 404], ['scope-hidden-workspace', 403]] as const) {
    await page.route(`**/v1/operator/workspaces/${slug}`, (route) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Workspace not found' }),
    }));
    await page.goto(`/app/workspaces/${slug}`);
    await expect(page).toHaveURL(`/app/workspaces/${slug}`);
    await expect(page.locator('[data-async-state="not_found"]')).toBeVisible();
    await expect(page.getByText('The requested resource is unavailable or outside your visible scope.')).toBeVisible();
    await expect(page.getByText(slug)).toHaveCount(0);
    await expect(page.locator('.operator-workspace-choice.selected')).toHaveCount(0);
    await expect(page.getByText('Workspace control room')).toHaveCount(0);
    await page.unroute(`**/v1/operator/workspaces/${slug}`);
  }
});

test('J11 Workspace tabs expose real tab semantics, arrow navigation, and distinct nested views', async ({ page }) => {
  await page.goto('/app/workspaces/e2e-workspace');
  const tabs = page.getByRole('tablist', { name: 'Workspace views' });
  const cases = [
    ['overview', 'Current task load'],
    ['work', 'durable tasks'],
    ['communication', 'Handoffs'],
    ['knowledge', 'Source lookup'],
    ['activity', 'Workspace timeline'],
    ['access', 'Visibility'],
  ] as const;
  await expect(tabs.getByRole('tab')).toHaveCount(cases.length);
  for (const [name, content] of cases) {
    const tab = tabs.getByRole('tab', { name });
    await tab.click();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tabpanel')).toContainText(content);
  }
  const access = tabs.getByRole('tab', { name: 'access' });
  await access.focus();
  await page.keyboard.press('ArrowRight');
  await expect(tabs.getByRole('tab', { name: 'overview' })).toBeFocused();
  await expect(tabs.getByRole('tab', { name: 'overview' })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('End');
  await expect(access).toBeFocused();
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

test('J11 a failed refresh replaces stale success with exclusive loading and error states', async ({ page }) => {
  let calls = 0;
  let releaseRefresh!: () => void;
  const held = new Promise<void>((resolve) => { releaseRefresh = resolve; });
  await page.route('**/v1/operator/governance', async (route) => {
    calls += 1;
    if (calls === 1) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          decisions: [{ code: 'STALE-DECISION', kind: 'approval', title: 'Stale decision', workspace: 'e2e-workspace', created_at: '2026-01-01T00:00:00Z' }],
          actions: [],
          audit: [],
          chain: null,
        }),
      });
      return;
    }
    await held;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic unavailable' }) });
  });
  await page.route('**/v1/operator/audit/verify', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ ok: true }),
  }));
  await page.goto('/app/governance');
  await expect(page.getByRole('heading', { name: 'Stale decision', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Verify audit chain' }).click();
  await expect(page.locator('[data-async-state="loading"]')).toBeVisible();
  await expect(page.getByText('Stale decision', { exact: true })).toHaveCount(0);
  await expect(page.locator('[data-async-state="success"]')).toHaveCount(0);
  releaseRefresh();
  await expect(page.locator('[data-async-state="error"]')).toBeVisible();
  await expect(page.getByText('Stale decision', { exact: true })).toHaveCount(0);
  await expect(page.locator('[data-async-state="success"]')).toHaveCount(0);
});

test('J11 a capability-catalog authorization failure exposes no stale actions', async ({ page }) => {
  let releaseCatalog!: () => void;
  const held = new Promise<void>((resolve) => { releaseCatalog = resolve; });
  await page.route('**/v1/operator/capabilities', async (route) => {
    await held;
    await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic denied' }) });
  });
  await page.goto('/app/act');
  await page.reload();
  await expect(page.locator('[data-async-state="loading"]')).toBeVisible();
  await expect(page.getByText('Operator jobs', { exact: true })).toHaveCount(0);
  await expect(page.locator('.operator-capability, .operator-action-sheet')).toHaveCount(0);
  releaseCatalog();
  await expect(page.locator('[data-async-state="unauthorized"]')).toBeVisible();
  await expect(page.locator('[data-async-state="success"]')).toHaveCount(0);
  await expect(page.getByText('Operator jobs', { exact: true })).toHaveCount(0);
  await expect(page.locator('.operator-capability, .operator-action-sheet')).toHaveCount(0);
});

test('J11 operator color tokens meet WCAG AA contrast for normal text roles', async ({ page }) => {
  await page.goto('/app/command-center');
  const ratios = await page.evaluate(() => {
    const styles = getComputedStyle(document.documentElement);
    const hex = (name: string) => styles.getPropertyValue(name).trim();
    const luminance = (color: string) => {
      const channels = color.match(/[a-f\d]{2}/gi)!.map((value) => parseInt(value, 16) / 255)
        .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    };
    const ratio = (foreground: string, background: string) => {
      const [lighter, darker] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
      return (lighter + 0.05) / (darker + 0.05);
    };
    return {
      ink: ratio(hex('--control-ink'), hex('--control-surface')),
      muted: ratio(hex('--control-muted'), hex('--control-surface')),
      blue: ratio(hex('--control-blue'), hex('--control-surface')),
      green: ratio(hex('--control-green'), hex('--control-surface')),
      amber: ratio(hex('--control-amber'), hex('--control-surface')),
      red: ratio(hex('--control-red'), hex('--control-surface')),
      nav: ratio(hex('--control-nav-text'), hex('--control-nav')),
      navMuted: ratio(hex('--control-nav-muted'), hex('--control-nav')),
    };
  });
  for (const [role, ratio] of Object.entries(ratios)) {
    expect(ratio, `${role} contrast ${ratio.toFixed(2)} is below 4.5:1`).toBeGreaterThanOrEqual(4.5);
  }
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
  const combobox = dialog.getByRole('combobox', { name: 'Find a view or typed action' });
  await expect(combobox).toBeFocused();
  await expect(combobox).toHaveAttribute('aria-expanded', 'true');
  await expect(combobox).toHaveAttribute('aria-controls', 'command-palette-results');
  await expect(combobox).toHaveAttribute('aria-activedescendant', 'command-palette-option-0');
  await page.keyboard.press('ArrowDown');
  await expect(combobox).toHaveAttribute('aria-activedescendant', 'command-palette-option-1');
  await expect(dialog.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('ArrowUp');

  await page.keyboard.press('Shift+Tab');
  await expect(dialog.getByRole('option').last()).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(combobox).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});
