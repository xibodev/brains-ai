import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test, expect, signIn } from '../fixtures/console.js';
import type { APIRequestContext, Locator, Page, TestInfo } from '@playwright/test';

/** Real main-app HTTP + SQLite. See fixtures/workspace_work.py for the isolated seed.
 * No global stack changes: use the existing BRAINS_E2E_BASE_URL/KEY convention.
 * Agent-only operations run through the core in the same disposable container.
 */
type World = {
  org: string; key: string; operator_id: number; peers: string[]; foreign_peer: string;
  alpha: { slug: string }; beta: { slug: string }; hidden: { slug: string };
};
const world = JSON.parse(process.env.BRAINS_E2E_WORK_MANIFEST ?? 'null') as World | null;
const driverPath = fileURLToPath(new URL('../fixtures/workspace_work.py', import.meta.url));
function driver(data: Record<string, unknown>) {
  return JSON.parse(execFileSync(process.env.BRAINS_E2E_PYTHON ?? 'python', [driverPath, JSON.stringify(data)], {
    encoding: 'utf8', env: process.env,
  }).trim());
}
const base = (slug = world!.alpha.slug) => `/v1/operator/workspaces/${slug}`;
const assignment = (page: Page) => page.getByRole('region', { name: 'Assignments', exact: true });
const deliberation = (page: Page) => page.getByRole('region', { name: 'Deliberations', exact: true });
const detail = (region: Locator) => region.locator('.workspace-work-detail');
async function openWork(page: Page, slug = world!.alpha.slug) {
  await page.goto(`/app/workspaces/${slug}`);
  await page.getByRole('tab', { name: 'work', exact: true }).click();
  await expect(assignment(page).getByRole('button', { name: 'New assignment' })).toBeEnabled();
}
async function post(request: APIRequestContext, url: string, data: unknown) {
  const response = await request.post(url, { data });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}
async function read(request: APIRequestContext, url: string) {
  const response = await request.get(url);
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}
async function createAssignment(page: Page, title: string) {
  const region = assignment(page);
  await region.getByRole('button', { name: 'New assignment' }).click();
  await region.getByLabel('Title (required)').fill(title);
  await region.getByLabel('Objective (required)').fill('Verify synthetic migration and evidence');
  await region.getByLabel('Context', { exact: false }).fill('Synthetic context only; no live infrastructure');
  const response = page.waitForResponse(r => r.url().endsWith('/assignments') && r.request().method() === 'POST');
  await region.getByRole('button', { name: 'Create assignment', exact: true }).click();
  const result = await response;
  expect(result.status(), await result.text()).toBe(200);
  const row = await result.json();
  await expect(detail(region).getByRole('heading', { name: title, exact: true })).toBeVisible();
  return row;
}
async function refresh(region: Locator) {
  await detail(region).getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(detail(region)).toHaveAttribute('aria-busy', 'false');
}
async function screenshot(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: 'image/png' });
}
async function semantics(page: Page) {
  await expect(page.getByRole('heading', { level: 1, name: 'Workspaces', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const panel = page.getByRole('tabpanel', { name: 'work', exact: true });
  const controls = panel.locator('button:visible:not(:disabled), input:visible:not(:disabled), textarea:visible:not(:disabled), select:visible:not(:disabled), a:visible');
  for (const control of await controls.all()) {
    await expect(control).toHaveAccessibleName(/\S/);
    await control.focus();
    await expect(control).toBeFocused();
    const box = await control.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(-1);
    expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  }
}

test.describe('Workspace Work — real operator browser and peer protocol', () => {
  test.skip(!world, 'Requires container-only workspace_work.py seed and BRAINS_E2E_WORK_MANIFEST');
  test.setTimeout(120_000);
  test.use({ viewport: { width: 1440, height: 1000 } });
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await expect(page).not.toHaveURL(/admin\/login/);
  });

  test('operator authors assignment without a Session, agent accepts, browser requests cooperative cancellation', async ({ page, consoleGuard }, info) => {
    await post(page.request, `${base()}/tasks`, { title: 'Synthetic prerequisite task', body: 'Local browser evidence', priority: 'p2' });
    await openWork(page);
    await expect(page.getByText('Synthetic prerequisite task', { exact: true })).toBeVisible();
    const before = driver({ action: 'sessions' });
    const row = await createAssignment(page, `Browser assignment ${info.testId}`);
    expect(row.creator_kind).toBe('operator');
    expect(row.creator_session_id).toBeNull();
    expect(row.creator_operator_id).toBe(world!.operator_id);
    expect(row.attempts).toEqual([]);
    expect(driver({ action: 'sessions' })).toEqual(before);
    const region = assignment(page);
    await expect(detail(region).getByText('No agent has accepted this assignment yet.')).toBeVisible();
    await expect(detail(region).getByText('Recorded as you · human operator')).toBeVisible();
    driver({ action: 'accept_assignment', code: row.code, revision: row.revision, session_id: world!.peers[0] });
    await refresh(region);
    await expect(detail(region).getByRole('heading', { name: 'Attempt 1 · codex' })).toBeVisible();
    const accepted = await read(page.request, `${base()}/assignments/${row.code}`);
    await detail(region).getByRole('button', { name: 'Request cancellation', exact: true }).click();
    const response = page.waitForResponse(r => r.url().endsWith(`/${row.code}/cancel`));
    await region.getByRole('button', { name: 'Confirm cancellation' }).click();
    const cancelled = await response;
    expect(cancelled.status()).toBe(200);
    expect(cancelled.request().postDataJSON()).toEqual({ expected_revision: accepted.revision });
    expect((await cancelled.json()).status).toBe('cancel_requested');
    await expect(detail(region).getByText(/not proof of a stopped process/)).toBeVisible();
    const attempt = (await read(page.request, `${base()}/assignments/${row.code}`)).attempts[0];
    expect(attempt.status).toBe('cancel_requested');
    expect(attempt.settled_at).toBeNull();
    await screenshot(page, info, 'desktop-operator-work');
    consoleGuard.assertClean();
  });

  test('two real agent acknowledgements, blinded reports, explicit HTTP advancement and preserved dissent', async ({ page, consoleGuard }, info) => {
    await openWork(page);
    const region = deliberation(page);
    await region.getByRole('button', { name: 'New proposal' }).click();
    await region.getByLabel('Title (required)').fill(`Browser deliberation ${info.testId}`);
    await region.getByLabel('Objective (required)').fill('Compare the synthetic options');
    await region.getByLabel('Context (required)').fill('Synthetic local context');
    await region.getByLabel('Evidence expectations (required)').fill('Cite the local synthetic probe');
    await expect(region.getByRole('checkbox')).toHaveCount(2);
    await expect(region.getByText(world!.foreign_peer)).toHaveCount(0);
    for (const id of world!.peers) await region.getByRole('checkbox', { name: new RegExp(id) }).check();
    await region.getByLabel('Result owner (required)').selectOption(world!.peers[0]);
    await region.getByLabel('Discussion rounds (required)').fill('0');
    const before = driver({ action: 'sessions' });
    const created = page.waitForResponse(r => r.url().endsWith('/coordinations') && r.request().method() === 'POST');
    await region.getByRole('button', { name: 'Create proposal', exact: true }).click();
    const creation = await created;
    expect(creation.status(), await creation.text()).toBe(200);
    let row = await creation.json();
    expect(row.creator_session_id).toBeNull();
    expect(row.creator_kind).toBe('operator');
    expect(row.accepted_session_ids).toEqual([]);
    expect(row.counts.acceptances).toBe(0);
    expect(driver({ action: 'sessions' })).toEqual(before);
    for (const session_id of world!.peers) driver({ action: 'accept_proposal', code: row.code, session_id });
    await refresh(region);
    await expect(detail(region).getByText(/2\/2 acknowledgements · 0\/2 initial reports/)).toBeVisible();
    const mutations: string[] = [];
    for (const phase of ['Begin initial collection', 'Close initial collection']) {
      const transition = page.waitForResponse(r => r.url().endsWith(`/${row.code}/advance`));
      await detail(region).getByRole('button', { name: phase, exact: true }).click();
      const response = await transition;
      expect(response.status(), await response.text()).toBe(200);
      row = await response.json();
      mutations.push(row.status);
      if (phase === 'Begin initial collection') {
        await expect(detail(region).getByRole('button', { name: 'Close initial collection' })).toBeDisabled();
        for (const [index, session_id] of world!.peers.entries()) {
          driver({ action: 'initial', code: row.code, session_id, payload: {
            findings: `WITHHELD_FINDINGS_${index}`, evidence: `WITHHELD_EVIDENCE_${index}`,
            uncertainty: 'Synthetic uncertainty', dissent: `ORIGINAL_DISSENT_${index}`,
          } });
          if (index === 0) {
            await refresh(region);
            await expect(detail(region).getByText(/1\/2 initial reports/)).toBeVisible();
            await expect(detail(region).getByRole('button', { name: 'Close initial collection' })).toBeDisabled();
            await expect(page.locator('body')).not.toContainText(/WITHHELD_|ORIGINAL_DISSENT/);
          }
        }
        await refresh(region);
        await expect(detail(region).getByText(/2\/2 initial reports/)).toBeVisible();
        await expect(page.locator('body')).not.toContainText(/WITHHELD_|ORIGINAL_DISSENT/);
        const blinded = await read(page.request, `${base()}/coordinations/${row.code}`);
        expect(blinded.blinded).toBe(true);
        expect(blinded.contributions).toEqual([]);
        expect(JSON.stringify(blinded)).not.toMatch(/WITHHELD_|ORIGINAL_DISSENT/);
      }
    }
    expect(mutations).toEqual(['collecting', 'discussing']);
    await expect(detail(region).getByText('WITHHELD_FINDINGS_0', { exact: true })).toBeVisible();
    await expect(detail(region).getByRole('heading', { name: 'Unresolved dissent' })).toBeVisible();
    expect(row.final_ready).toBe(true);
    driver({ action: 'final', code: row.code, session_id: world!.peers[0], payload: {
      summary: 'Synthetic result-owner synthesis', evidence: 'Synthetic final evidence',
    } });
    await refresh(region);
    await expect(detail(region).getByText('Synthetic result-owner synthesis', { exact: true })).toBeVisible();
    await expect(detail(region).getByText('ORIGINAL_DISSENT_0', { exact: true })).toHaveCount(2);
    await detail(region).getByLabel(/Historical version/).fill('1');
    await detail(region).getByRole('button', { name: 'View version', exact: true }).click();
    await expect(detail(region).getByText(/Viewing version 1 in read-only mode/)).toBeVisible();
    await expect(detail(region).getByRole('button', { name: 'Advance protocol', exact: true })).toBeDisabled();
    await screenshot(page, info, 'desktop-deliberation-evidence');
    consoleGuard.assertClean();
  });

  test('stale browser cancellation receives real 409, refreshes and requires re-review', async ({ page }, info) => {
    await openWork(page);
    const row = await createAssignment(page, `Stale assignment ${info.testId}`);
    const region = assignment(page);
    await post(page.request, `${base()}/assignments/${row.code}/cancel`, { expected_revision: row.revision });
    await detail(region).getByRole('button', { name: 'Request cancellation', exact: true }).click();
    const conflict = page.waitForResponse(r => r.url().endsWith(`/${row.code}/cancel`));
    await region.getByRole('button', { name: 'Confirm cancellation' }).click();
    expect((await conflict).status()).toBe(409);
    await expect(detail(region).getByText(/Nothing was automatically retried/)).toBeVisible();
    await expect(detail(region).getByText('Cancelled', { exact: true })).toBeVisible();
    await detail(region).getByRole('button', { name: 'I reviewed the refreshed record' }).click();
    await expect(detail(region).getByRole('alert')).toHaveCount(0);
    await expect(detail(region).getByRole('button', { name: 'Request cancellation', exact: true })).toBeDisabled();
  });

  test('lost creation response retries the same idempotency key after real server commit', async ({ page }, info) => {
    await openWork(page);
    const region = assignment(page);
    const keys: string[] = [];
    let committedCode = '';
    await page.route(`**${base()}/assignments`, async route => {
      if (route.request().method() !== 'POST') return route.continue();
      keys.push(route.request().postDataJSON().idempotency_key);
      const response = await route.fetch();
      expect(response.status(), await response.text()).toBe(200);
      committedCode = (await response.json()).code;
      if (keys.length === 1) await route.abort('connectionreset');
      else await route.fulfill({ response });
    });
    await region.getByRole('button', { name: 'New assignment' }).click();
    const title = `Lost response ${info.testId}`;
    await region.getByLabel('Title (required)').fill(title);
    await region.getByLabel('Objective (required)').fill('One immutable assignment');
    await region.getByRole('button', { name: 'Create assignment', exact: true }).click();
    await expect(region.getByRole('alert')).toBeVisible();
    expect(committedCode).not.toBe('');
    await region.getByRole('button', { name: 'Create assignment', exact: true }).click();
    await expect(detail(region).getByRole('heading', { name: title })).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    const rows = (await read(page.request, `${base()}/assignments`)).items.filter((r: { title: string }) => r.title === title);
    expect(rows).toHaveLength(1);
    expect(rows[0].code).toBe(committedCode);
  });

  test('slow old-workspace response is aborted and cannot bleed into the new workspace', async ({ page }, info) => {
    await openWork(page);
    const row = await createAssignment(page, `Alpha-only ${info.testId}`);
    let release!: () => void;
    let reached!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const fetched = new Promise<void>(resolve => { reached = resolve; });
    const aborted = page.waitForEvent('requestfailed', { predicate: r => r.url().includes(`${base()}/assignments?`) });
    await page.route(`**${base()}/assignments?*`, async route => {
      const response = await route.fetch();
      reached();
      await held;
      await route.fulfill({ response }).catch(() => {});
    });
    await assignment(page).getByRole('button', { name: 'Refresh', exact: true }).first().click();
    await fetched;
    await page.getByRole('button', { name: /Synthetic beta/ }).click();
    await page.getByRole('tab', { name: 'work', exact: true }).click();
    release();
    expect((await aborted).failure()?.errorText).toMatch(/ABORTED/);
    await expect(assignment(page).getByText('No assignments yet')).toBeVisible();
    await expect(page.locator('body')).not.toContainText(row.title);
    await expect(page.locator('body')).not.toContainText(world!.hidden.slug);
  });

  test('mobile cards, forms and details have labels, focus, keyboard tabs and no horizontal overflow', async ({ page, consoleGuard }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openWork(page);
    const work = page.getByRole('tab', { name: 'work', exact: true });
    await work.focus();
    await page.keyboard.press('ArrowLeft');
    await expect(page.getByRole('tab', { name: 'overview', exact: true })).toBeFocused();
    await page.keyboard.press('ArrowRight');
    await expect(work).toBeFocused();
    await expect(work).toHaveAttribute('aria-selected', 'true');
    await createAssignment(page, `Mobile assignment ${info.testId}`);
    await semantics(page);
    await screenshot(page, info, 'mobile-cards-details');
    const region = deliberation(page);
    await region.getByRole('button', { name: 'New proposal' }).click();
    await expect(region.getByLabel('Title (required)')).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(region.getByLabel('Objective (required)')).toBeFocused();
    for (const id of world!.peers) await region.getByRole('checkbox', { name: new RegExp(id) }).check();
    await semantics(page);
    await screenshot(page, info, 'mobile-proposal-form');
    consoleGuard.assertClean();
  });

  for (const width of [1440, 390]) {
    for (const family of ['assignment', 'proposal'] as const) {
      test(`${family} form restores keyboard focus after closing at ${width}px`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await openWork(page);
        const region = family === 'assignment' ? assignment(page) : deliberation(page);
        const trigger = region.getByRole('button', { name: family === 'assignment' ? 'New assignment' : 'New proposal' });
        await trigger.focus();
        await page.keyboard.press('Enter');
        await expect(region.getByLabel('Title (required)')).toBeFocused();
        await region.getByRole('button', { name: 'Close form' }).click();
        await expect(trigger).toBeEnabled();
        await info.attach('focus-after-close', { body: JSON.stringify(await page.evaluate(() => ({
          tag: document.activeElement?.tagName,
          label: document.activeElement?.getAttribute('aria-label'),
          text: document.activeElement?.textContent?.slice(0, 160),
        }))), contentType: 'application/json' });
        await expect(trigger).toBeFocused();
      });
    }
  }

  test('real empty and hidden/missing pages; injected pending, 403, 404 and server error stay explicit', async ({ page }) => {
    await openWork(page, world!.beta.slug);
    await expect(assignment(page).getByText('No assignments yet')).toBeVisible();
    await expect(deliberation(page).getByText('No deliberations yet')).toBeVisible();
    for (const slug of [world!.hidden.slug, 'synthetic-does-not-exist']) {
      await page.goto(`/app/workspaces/${slug}`);
      await expect(page.locator('[data-boundary="workspace-detail"]')).toHaveAttribute('data-async-state', 'not_found');
      await expect(page.getByRole('tab', { name: 'work', exact: true })).toHaveCount(0);
    }
    for (const status of [403, 404, 503]) {
      let release!: () => void;
      const held = new Promise<void>(resolve => { release = resolve; });
      await page.route(`**${base()}/assignments?*`, async route => {
        await held;
        await route.fulfill({ status, json: { detail: 'SENSITIVE_SYNTHETIC_ERROR' } });
      });
      await openWork(page);
      await expect(assignment(page).locator('[data-async-state="loading"]')).toBeVisible();
      release();
      await expect(assignment(page).locator(`[data-async-state="${status === 403 ? 'unauthorized' : status === 404 ? 'not_found' : 'error'}"]`)).toBeVisible();
      await expect(page.locator('body')).not.toContainText('SENSITIVE_SYNTHETIC_ERROR');
      await page.unroute(`**${base()}/assignments?*`);
    }
  });

  test('raw API key can read but cannot perform operator work mutations', async ({ page, playwright }) => {
    const raw = await playwright.request.newContext({
      baseURL: process.env.BRAINS_E2E_BASE_URL, extraHTTPHeaders: { Authorization: `Bearer ${world!.key}` },
    });
    try {
      expect((await raw.get(`${base()}/assignments`)).status()).toBe(200);
      const response = await raw.post(`${base()}/assignments`, { data: {
        title: 'Raw key must not create', specification: { objective: 'Synthetic' }, idempotency_key: 'raw-key-denied',
      } });
      expect(response.status()).toBe(403);
      const rows = await read(page.request, `${base()}/assignments`);
      expect(JSON.stringify(rows)).not.toContain('Raw key must not create');
    } finally { await raw.dispose(); }
  });

  for (const width of [1440, 390]) {
    test(`lost proposal response retains an unavailable owner for unchanged retry only at ${width}px`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      // Each case owns its disposable result owner; earlier journeys' peers stay live.
      const owner = driver({ action: 'start_retry_owner', session_id: world!.peers[0] }).session_id as string;
      const title = `Unavailable-owner retry ${width}`;
      const requests: Record<string, unknown>[] = [];
      let committedCode = '';
      let ownerEnded = false;
      const editedPage = await page.context().newPage();
      const fillProposal = async (target: Page, name: string) => {
        await openWork(target);
        const region = deliberation(target);
        await region.getByRole('button', { name: 'New proposal' }).click();
        await region.getByLabel('Title (required)').fill(name);
        await region.getByLabel('Objective (required)').fill('Recover exactly one committed proposal');
        await region.getByLabel('Context (required)').fill('Synthetic retry context');
        await region.getByLabel('Evidence expectations (required)').fill('Real HTTP commit and identical retry payload');
        for (const id of [owner, world!.peers[1]]) await region.getByRole('checkbox', { name: new RegExp(id) }).check();
        await region.getByLabel('Result owner (required)').selectOption(owner);
        await region.getByLabel('Discussion rounds (required)').fill('0');
        return region;
      };
      try {
        const region = await fillProposal(page, title);
        const edited = await fillProposal(editedPage, `${title} new request`);
        let editedPosts = 0;
        editedPage.on('request', request => {
          if (request.method() === 'POST' && request.url().endsWith('/coordinations')) editedPosts += 1;
        });
        await page.route(`**${base()}/coordinations`, async route => {
          if (route.request().method() !== 'POST') return route.continue();
          requests.push(route.request().postDataJSON());
          const response = await route.fetch();
          expect(response.status(), await response.text()).toBe(200);
          const row = await response.json();
          if (requests.length === 1) {
            committedCode = row.code;
            await route.abort('connectionreset');
          } else {
            expect(row.code).toBe(committedCode);
            await route.fulfill({ response });
          }
        });
        await region.getByRole('button', { name: 'Create proposal', exact: true }).click();
        await expect(region.getByText(/If the response was lost/)).toBeVisible();
        expect(committedCode).not.toBe('');
        expect((await read(page.request, `${base()}/coordinations/${committedCode}`)).code).toBe(committedCode);
        expect(driver({ action: 'end_session', session_id: owner }).ok).toBe(true);
        ownerEnded = true;
        for (const target of [region, edited]) {
          await target.getByRole('group', { name: 'Participants (required · choose 2–8)' }).getByRole('button', { name: 'Refresh', exact: true }).click();
          await expect(target.getByRole('checkbox', { name: new RegExp(owner) })).toHaveCount(0);
          await expect(target.getByText(/Some selected Sessions are no longer available/)).toBeVisible();
        }
        const ownerSelect = region.getByLabel('Result owner (required)');
        await expect(ownerSelect).toHaveValue(owner);
        await expect(ownerSelect.locator('option:checked')).toContainText('retry only');
        expect(await ownerSelect.evaluate(el => (el as HTMLSelectElement).validity.valid)).toBe(true);
        await expect(region.getByRole('button', { name: 'Create proposal', exact: true })).toBeEnabled();
        const path = info.outputPath(`unavailable-owner-retry-${width}.png`);
        await ownerSelect.locator('..').screenshot({ path });
        await info.attach('unavailable-owner-select', { path, contentType: 'image/png' });
        const retry = page.waitForResponse(r => r.url().endsWith('/coordinations') && r.request().method() === 'POST');
        await region.getByRole('button', { name: 'Create proposal', exact: true }).click();
        expect((await retry).status()).toBe(200);
        await expect(detail(region).getByRole('heading', { name: title, exact: true })).toBeVisible();
        expect(requests).toHaveLength(2);
        expect(requests[1]).toEqual(requests[0]);
        expect(requests[0].idempotency_key).toEqual(expect.any(String));
        const rows = (await read(page.request, `${base()}/coordinations`)).items.filter((r: { title: string }) => r.title === title);
        expect(rows).toHaveLength(1);
        expect(rows[0].code).toBe(committedCode);
        expect(rows[0].result_owner_session_id).toBe(owner);
        // The independently opened form has no replay key: editing cannot borrow
        // the retained unavailable option to authorize a new proposal.
        await edited.getByLabel('Objective (required)').fill('Edited new request with ended owner');
        await expect(edited.getByRole('button', { name: 'Create proposal', exact: true })).toBeDisabled();
        await edited.locator('form.workspace-work-form').evaluate(form => (form as HTMLFormElement).requestSubmit());
        await expect(edited.getByText(/Choose 2–8 distinct Sessions from the current owned-participant list/)).toBeVisible();
        expect(editedPosts).toBe(0);
        await info.attach('retry-proof', { body: JSON.stringify({
          code: committedCode, owner, identicalRequests: requests[0], editedPosts,
        }), contentType: 'application/json' });
      } finally {
        await editedPage.close();
        if (!ownerEnded) driver({ action: 'end_session', session_id: owner });
      }
    });
  }
});
