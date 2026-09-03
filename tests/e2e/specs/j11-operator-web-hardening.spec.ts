import { test, expect, signIn } from '../fixtures/console.js';
import type { Page } from '@playwright/test';
import { seedMailboxJourney } from '../fixtures/seed.js';

test.beforeAll(() => {
  seedMailboxJourney();
});

type BoundaryOutcome = 'empty' | 'error' | 'unauthorized';

async function exerciseBoundary(
  page: Page,
  options: {
    boundary: string;
    endpoint: string;
    emptyBody: unknown;
    open: () => Promise<void>;
    unauthorizedState?: 'unauthorized' | 'not_found';
  },
) {
  for (const outcome of ['empty', 'error', 'unauthorized'] as BoundaryOutcome[]) {
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    await page.route(options.endpoint, async (route) => {
      await held;
      const status = outcome === 'empty' ? 200 : outcome === 'unauthorized' ? 403 : 503;
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(outcome === 'empty' ? options.emptyBody : { detail: 'SENSITIVE-BACKEND-DETAIL' }),
      });
    });
    await options.open();
    const boundary = page.locator(`[data-boundary="${options.boundary}"]`);
    await expect(boundary).toHaveAttribute('data-async-state', 'loading');
    expect(await renderedContrastViolations(page), `${options.boundary} loading contrast`).toEqual([]);
    release();
    const expected = outcome === 'unauthorized' ? (options.unauthorizedState ?? 'unauthorized') : outcome;
    await expect(boundary).toHaveAttribute('data-async-state', expected);
    expect(await renderedContrastViolations(page), `${options.boundary} ${expected} contrast`).toEqual([]);
    await expect(page.getByText('SENSITIVE-BACKEND-DETAIL')).toHaveCount(0);
    await page.unroute(options.endpoint);
    await page.goto('/app/command-center');
  }
}

async function renderedContrastViolations(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    type Color = { r: number; g: number; b: number; a: number };
    const parse = (value: string): Color | null => {
      const parts = value.match(/[\d.]+/g)?.map(Number);
      if (!parts || parts.length < 3) return null;
      return { r: parts[0], g: parts[1], b: parts[2], a: parts[3] ?? 1 };
    };
    const over = (top: Color, bottom: Color): Color => {
      const alpha = top.a + bottom.a * (1 - top.a);
      if (alpha === 0) return { r: 0, g: 0, b: 0, a: 0 };
      return {
        r: (top.r * top.a + bottom.r * bottom.a * (1 - top.a)) / alpha,
        g: (top.g * top.a + bottom.g * bottom.a * (1 - top.a)) / alpha,
        b: (top.b * top.a + bottom.b * bottom.a * (1 - top.a)) / alpha,
        a: alpha,
      };
    };
    const luminance = (color: Color) => {
      const channel = (value: number) => {
        const normalized = value / 255;
        return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
    };
    const contrast = (first: Color, second: Color) => {
      const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a);
      return (lighter + 0.05) / (darker + 0.05);
    };
    const background = (element: HTMLElement) => {
      let composed: Color = { r: 0, g: 0, b: 0, a: 0 };
      for (let current: HTMLElement | null = element; current; current = current.parentElement) {
        const layer = parse(getComputedStyle(current).backgroundColor);
        if (layer) composed = over(composed, layer);
      }
      return over(composed, { r: 255, g: 255, b: 255, a: 1 });
    };
    const candidates = Array.from(document.querySelectorAll<HTMLElement>('body *')).filter((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const hasDirectText = Array.from(element.childNodes).some((node) => node.nodeType === Node.TEXT_NODE && Boolean(node.textContent?.trim()));
      const hasTextualInputValue = element instanceof HTMLInputElement
        && !['checkbox', 'radio', 'range', 'color', 'file', 'image', 'button', 'submit', 'reset', 'hidden'].includes(element.type)
        && Boolean(element.value || element.placeholder);
      const hasFormText = hasTextualInputValue
        || (element instanceof HTMLTextAreaElement && Boolean(element.value || element.placeholder));
      return (hasDirectText || hasFormText) && !element.closest(':disabled') && rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    });
    return candidates.flatMap((element, index) => {
      const usesPlaceholder = (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)
        && !element.value && Boolean(element.placeholder);
      const style = getComputedStyle(element, usesPlaceholder ? '::placeholder' : null);
      const foreground = parse(style.color);
      if (!foreground) return [];
      let opacity = foreground.a;
      for (let current: HTMLElement | null = element; current; current = current.parentElement) opacity *= Number(getComputedStyle(current).opacity || 1);
      foreground.a = opacity;
      const behind = background(element);
      const rendered = over(foreground, behind);
      const ratio = contrast(rendered, behind);
      const size = Number.parseFloat(style.fontSize);
      const weight = Number.parseInt(style.fontWeight, 10) || (style.fontWeight === 'bold' ? 700 : 400);
      const threshold = size >= 24 || (size >= 18.66 && weight >= 700) ? 3 : 4.5;
      return ratio + 0.01 < threshold
        ? [`${index}:${element.tagName}.${element.className} ${ratio.toFixed(2)} < ${threshold}`]
        : [];
    });
  });
}

async function assertResponsiveControls(page: Page, route: string, viewport: { width: number; height: number }) {
  const controls = page.locator('a:visible, button:visible:not(:disabled), input:visible:not(:disabled), select:visible:not(:disabled), textarea:visible:not(:disabled)');
  for (let index = 0; index < await controls.count(); index += 1) {
    const control = controls.nth(index);
    await control.focus();
    const failures = await control.evaluate((element, controlIndex) => {
      const node = element as HTMLElement;
      const rect = node.getBoundingClientRect();
      const issues: string[] = [];
      const label = node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent?.trim().slice(0, 48) || node.tagName;
      if (node.matches(':disabled') || getComputedStyle(node).pointerEvents === 'none') issues.push(`control ${controlIndex} (${label}) is disabled`);
      if (rect.width <= 0 || rect.height <= 0) issues.push(`control ${controlIndex} (${label}) has no rendered box`);
      if (rect.left < -1 || rect.right > window.innerWidth + 1 || rect.bottom <= 0 || rect.top >= window.innerHeight) {
        issues.push(`control ${controlIndex} (${label}) does not intersect the viewport horizontally and vertically`);
      }
      for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        const clipX = /(hidden|clip|auto|scroll)/.test(style.overflowX);
        const clipY = /(hidden|clip|auto|scroll)/.test(style.overflowY);
        if (!clipX && !clipY) continue;
        const boundary = ancestor.getBoundingClientRect();
        const intersectsX = rect.right > boundary.left && rect.left < boundary.right;
        const intersectsY = rect.bottom > boundary.top && rect.top < boundary.bottom;
        if ((clipX && !intersectsX) || (clipY && !intersectsY)) {
          issues.push(`control ${controlIndex} (${label}) does not intersect overflow ancestor ${ancestor.className || ancestor.tagName}`);
          break;
        }
        if ((clipX && (rect.left < boundary.left - 1 || rect.right > boundary.right + 1))
          || (clipY && (rect.top < boundary.top - 1 || rect.bottom > boundary.bottom + 1))) {
          issues.push(`control ${controlIndex} (${label}) is clipped by overflow ancestor ${ancestor.className || ancestor.tagName}`);
          break;
        }
      }
      const centerX = rect.left + rect.width / 2;
      const centerY = rect.top + rect.height / 2;
      if (centerX >= 0 && centerX < window.innerWidth && centerY >= 0 && centerY < window.innerHeight) {
        const hit = document.elementFromPoint(centerX, centerY);
        if (!hit || (hit !== node && !node.contains(hit))) issues.push(`control ${controlIndex} (${label}) is occluded at its center by ${(hit as HTMLElement | null)?.className || hit?.tagName || 'nothing'}`);
      }
      return issues;
    }, index);
    expect(failures, `${route} control ${index} must be reachable without scripted scrolling`).toEqual([]);
    await control.click({ trial: true });
  }
}

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
      const pageWidth = await page.evaluate(() => document.documentElement.clientWidth);
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      expect(scrollWidth, `${route.path} document overflow is not allowed`).toBeLessThanOrEqual(pageWidth + 1);
      const containers = page.locator('.control-shell, .control-content, .control-topbar, .control-main, .control-mobile-nav:visible, .operator-page, .operator-card, .operator-action-sheet, .masterdetail');
      for (let index = 0; index < await containers.count(); index += 1) {
        const box = await containers.nth(index).boundingBox();
        if (!box) continue;
        expect(box.width, `${route.path} container ${index} exceeded the viewport`).toBeLessThanOrEqual(viewport.width + 1);
        expect(box.x, `${route.path} container ${index} started outside the viewport`).toBeGreaterThanOrEqual(-1);
        expect(box.x + box.width, `${route.path} container ${index} ended outside the viewport`).toBeLessThanOrEqual(viewport.width + 1);
      }
      await assertResponsiveControls(page, route.path, viewport);
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
  expect(await renderedContrastViolations(page), 'unknown route contrast').toEqual([]);

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

test('J11 page and configuration boundaries expose loading, empty, error, and unauthorized exclusively', async ({ page }) => {
  const cases = [
    {
      boundary: 'coordination',
      endpoint: '**/v1/operator/coordination',
      emptyBody: { tasks: [], claims: [], handoffs: [], knowledge: [], signals: [] },
      route: '/app/coordination',
    },
    {
      boundary: 'governance',
      endpoint: '**/v1/operator/governance',
      emptyBody: { decisions: [], actions: [], audit: [], chain: null },
      route: '/app/governance',
    },
    {
      boundary: 'operations',
      endpoint: '**/v1/operator/operations',
      emptyBody: {},
      route: '/app/operations',
    },
    {
      boundary: 'config-local',
      endpoint: '**/v1/operator/configuration',
      emptyBody: { revision: 'synthetic', fields: [], harnesses: [], redaction: 'applied' },
      route: '/app/operations/config/local',
    },
    {
      boundary: 'config-health',
      endpoint: '**/v1/admin/readiness',
      emptyBody: { status: 'ready', components: {} },
      route: '/app/operations/config/health',
    },
    {
      boundary: 'act-workspaces',
      endpoint: '**/v1/operator/workspaces',
      emptyBody: { data: [] },
      route: '/app/act',
    },
  ] as const;
  for (const item of cases) {
    await exerciseBoundary(page, {
      ...item,
      open: () => page.goto(item.route).then(() => undefined),
    });
  }
});

test('J11 Workspace list exposes loading, empty, error, and unauthorized without backend detail', async ({ page }) => {
  await exerciseBoundary(page, {
    boundary: 'workspace-list',
    endpoint: '**/v1/operator/workspaces',
    emptyBody: { data: [] },
    open: () => page.goto('/app/workspaces').then(() => undefined),
  });
});

test('J11 Workspace detail exposes loading, empty, error, and privacy-preserving denied state', async ({ page }) => {
  await exerciseBoundary(page, {
    boundary: 'workspace-detail',
    endpoint: '**/v1/operator/workspaces/e2e-workspace',
    emptyBody: {},
    unauthorizedState: 'not_found',
    open: () => page.goto('/app/workspaces/e2e-workspace').then(() => undefined),
  });
  await expect(page.getByText('SENSITIVE-BACKEND-DETAIL')).toHaveCount(0);
});

test('J11 capability catalog exposes loading, empty, error, and unauthorized without stale actions', async ({ page }) => {
  await exerciseBoundary(page, {
    boundary: 'act-catalog',
    endpoint: '**/v1/operator/capabilities',
    emptyBody: { data: [], labs_enabled: false, install_admin: false },
    open: () => page.goto('/app/act').then(() => undefined),
  });
  await expect(page.locator('.operator-capability, .operator-action-sheet')).toHaveCount(0);
});

test('J11 mailbox access and message boundaries expose every typed state without backend detail', async ({ page }) => {
  await exerciseBoundary(page, {
    boundary: 'mail-access',
    endpoint: '**/v1/operator/mailboxes/access',
    emptyBody: { data: [] },
    open: () => page.goto('/app/coordination').then(() => undefined),
  });
  await exerciseBoundary(page, {
    boundary: 'mail-messages',
    endpoint: '**/v1/operator/mailboxes/inbox**',
    emptyBody: { mailbox: 'synthetic', cursor: 0, unread_count: 0, messages: [] },
    open: () => page.goto('/app/coordination').then(() => undefined),
  });
});

test('J11 mail composer Workspace boundary exposes every typed state without backend detail', async ({ page }) => {
  await exerciseBoundary(page, {
    boundary: 'mail-compose-workspaces',
    endpoint: '**/v1/operator/workspaces',
    emptyBody: { data: [] },
    open: async () => {
      await page.goto('/app/coordination');
      await page.getByRole('button', { name: 'Compose mail' }).click();
    },
  });
});

test('J11 mail address book exposes every typed state without backend detail', async ({ page }) => {
  await exerciseBoundary(page, {
    boundary: 'mail-address-book',
    endpoint: '**/v1/operator/mailboxes?workspace=*',
    emptyBody: { data: [] },
    open: async () => {
      await page.goto('/app/coordination');
      await page.getByRole('button', { name: 'Compose mail' }).click();
      await expect(page.locator('[data-boundary="mail-compose-workspaces"]')).toHaveAttribute('data-async-state', 'success');
    },
  });
});

test('J11 mailbox thread boundary exposes loading, empty, error, and unauthorized', async ({ page }) => {
  for (const outcome of ['empty', 'error', 'unauthorized'] as BoundaryOutcome[]) {
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    await page.route('**/v1/operator/mailboxes/threads/**', async (route) => {
      await held;
      const status = outcome === 'empty' ? 200 : outcome === 'unauthorized' ? 403 : 503;
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(outcome === 'empty' ? {
          thread_id: 'synthetic', origin_workspace: 'e2e-workspace', started_by: 'synthetic',
          subject: 'Synthetic thread', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
          mailbox: 'synthetic', unread_count: 0, cursor: 0, messages: [],
        } : { detail: 'SENSITIVE-BACKEND-DETAIL' }),
      });
    });
    await page.goto('/app/coordination');
    await page.locator('.operator-mail-conversations button').first().click();
    const boundary = page.locator('[data-boundary="mail-thread"]');
    await expect(boundary).toHaveAttribute('data-async-state', 'loading');
    release();
    await expect(boundary).toHaveAttribute('data-async-state', outcome);
    expect(await renderedContrastViolations(page), `mail-thread ${outcome} contrast`).toEqual([]);
    await expect(page.getByText('SENSITIVE-BACKEND-DETAIL')).toHaveCount(0);
    await page.unroute('**/v1/operator/mailboxes/threads/**');
  }
});

test('J11 source lookup boundary exposes loading, empty, error, and unauthorized', async ({ page }) => {
  for (const outcome of ['empty', 'error', 'unauthorized'] as BoundaryOutcome[]) {
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    await page.route('**/v1/operator/workspaces/e2e-workspace/lookup**', async (route) => {
      await held;
      const status = outcome === 'empty' ? 200 : outcome === 'unauthorized' ? 403 : 503;
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(outcome === 'empty' ? {
          status: 'empty', reason: 'no_matches', query: 'synthetic', results: [], scanned_files: 1,
          truncated: false, incomplete_reasons: [],
        } : { detail: 'SENSITIVE-BACKEND-DETAIL' }),
      });
    });
    await page.goto('/app/workspaces/e2e-workspace');
    await page.getByRole('tab', { name: 'knowledge' }).click();
    await page.getByLabel('Source query').fill('synthetic');
    await page.getByRole('button', { name: 'Lookup', exact: true }).click();
    const boundary = page.locator('[data-boundary="lookup"]');
    await expect(boundary).toHaveAttribute('data-async-state', 'loading');
    release();
    await expect(boundary).toHaveAttribute('data-async-state', outcome);
    expect(await renderedContrastViolations(page), `lookup ${outcome} contrast`).toEqual([]);
    await expect(page.getByText('SENSITIVE-BACKEND-DETAIL')).toHaveCount(0);
    await page.unroute('**/v1/operator/workspaces/e2e-workspace/lookup**');
  }
});

test('J11 mailbox folder tabs use roving focus and associated panels', async ({ page }) => {
  await page.goto('/app/coordination');
  const tabs = page.getByRole('tablist', { name: 'Mailbox folders' });
  const inbox = tabs.getByRole('tab', { name: /Inbox/ });
  const sent = tabs.getByRole('tab', { name: 'Sent' });
  await expect(inbox).toHaveAttribute('tabindex', '0');
  await expect(sent).toHaveAttribute('tabindex', '-1');
  await inbox.focus();
  await page.keyboard.press('ArrowRight');
  await expect(sent).toBeFocused();
  await expect(sent).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#mail-folder-panel-sent')).toBeVisible();
  await page.keyboard.press('Home');
  await expect(inbox).toBeFocused();
  await expect(page.locator('#mail-folder-panel-inbox')).toBeVisible();
  await page.keyboard.press('End');
  await expect(sent).toBeFocused();
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
  const catalogBoundary = page.locator('[data-boundary="act-catalog"]');
  await expect(catalogBoundary).toHaveAttribute('data-async-state', 'loading');
  await expect(page.getByText('Operator jobs', { exact: true })).toHaveCount(0);
  await expect(page.locator('.operator-capability, .operator-action-sheet')).toHaveCount(0);
  releaseCatalog();
  await expect(catalogBoundary).toHaveAttribute('data-async-state', 'unauthorized');
  await expect(page.getByText('Operator jobs', { exact: true })).toHaveCount(0);
  await expect(page.locator('.operator-capability, .operator-action-sheet')).toHaveCount(0);
});

test('J11 rendered text meets WCAG AA across every supported route and interactive state', async ({ page }) => {
  for (const viewport of [{ width: 1366, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    for (const route of CORE_ROUTES) {
      await page.goto(route.path);
      expect(await renderedContrastViolations(page), `${route.path} normal at ${viewport.width}px`).toEqual([]);
      const interactive = page.locator('a:visible, button:visible:not(:disabled), input:visible:not(:disabled), select:visible:not(:disabled), textarea:visible:not(:disabled)');
      for (let index = 0; index < await interactive.count(); index += 1) {
        const control = interactive.nth(index);
        await control.focus();
        expect(await renderedContrastViolations(page), `${route.path} control ${index} focus at ${viewport.width}px`).toEqual([]);
        await control.hover();
        expect(await renderedContrastViolations(page), `${route.path} control ${index} hover at ${viewport.width}px`).toEqual([]);
      }
    }
  }
});

test('J11 realtime loss is a visible degraded state while durable HTTP remains available', async ({ page }) => {
  await page.routeWebSocket('**/v1/ws', (socket) => {
    socket.close({ code: 1012, reason: 'synthetic restart' });
  });
  await page.goto('/app/command-center');
  await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  await expect(page.locator('[data-connection-state="degraded"]')).toContainText('Durable HTTP state remains available');
  expect(await renderedContrastViolations(page), 'degraded realtime contrast').toEqual([]);
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
  expect(await renderedContrastViolations(page), 'command palette normal contrast').toEqual([]);
  const paletteControls = dialog.locator('button:visible:not(:disabled), input:visible:not(:disabled)');
  for (let index = 0; index < await paletteControls.count(); index += 1) {
    await paletteControls.nth(index).focus();
    expect(await renderedContrastViolations(page), `command palette control ${index} focus contrast`).toEqual([]);
    await paletteControls.nth(index).hover();
    expect(await renderedContrastViolations(page), `command palette control ${index} hover contrast`).toEqual([]);
  }
  await combobox.focus();
  await page.mouse.move(0, 0);
  await combobox.fill('zz-no-match');
  await combobox.fill('');
  await expect(combobox).toHaveAttribute('aria-activedescendant', 'command-palette-option-0');
  await page.keyboard.press('ArrowDown');
  await expect(combobox).toHaveAttribute('aria-activedescendant', 'command-palette-option-1');
  await expect(dialog.getByRole('option').nth(1)).toHaveAttribute('aria-selected', 'true');
  await expect(dialog.getByRole('option').nth(1)).toHaveAttribute('tabindex', '-1');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Tab');
  await expect(combobox).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(combobox).toBeFocused();
  await combobox.fill('Workspaces');
  await expect(combobox).toHaveAttribute('aria-activedescendant', 'command-palette-option-0');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL('/app/workspaces');

  const nextOpener = page.getByRole('button', { name: /Search workspaces and actions/i });
  await nextOpener.click();
  await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeHidden();
  await expect(nextOpener).toBeFocused();
});
