import { test, expect, signIn } from '../fixtures/console.js';
import { seedApproval } from '../fixtures/seed.js';

/**
 * J8 — Ask, approve, steer, chat, and stop.
 *
 * Authority: F3, AC-F3-04 through AC-F3-07, and AC-B4-01 through AC-B4-04.
 * Browser coverage is limited to advertised governance and fail-closed behavior
 * for withdrawn execution controls.
 */

let approvalCode = '';

test.beforeAll(() => {
  approvalCode = String(seedApproval().code);
});

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J8.1 a pending governed action is approved once from Governance', async ({ page, consoleGuard }) => {
  await page.goto('/app/governance');
  const card = page.locator('.operator-decision-card', { hasText: approvalCode });
  await expect(card).toBeVisible();
  const [response] = await Promise.all([
    page.waitForResponse((resp) => resp.url().endsWith(`/approvals/${approvalCode}/resolve`)),
    card.getByRole('button', { name: /approve/i }).click(),
  ]);
  expect(response.ok(), `approval failed: ${response.status()}`).toBeTruthy();
  await expect(card).toHaveCount(0);

  const resolved = await page.request.get(`/v1/approvals/${approvalCode}`);
  expect(resolved.ok()).toBeTruthy();
  expect((await resolved.json()).status).toBe('resolved');
  consoleGuard.assertClean();
});

test('J8.2 withdrawn execution-control routes fail closed while governance remains available', async ({ page, consoleGuard }) => {
  for (const route of [
    '/app/sessions',
    '/app/sessions/session-1',
    '/app/labs/sessions',
    '/app/labs/sessions/session-1',
  ]) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }

  await page.goto('/app/governance');
  await expect(page.getByRole('heading', { name: /governance/i })).toBeVisible();
  await expect(page.getByRole('button', { name: /verify audit chain/i })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  consoleGuard.assertClean();
});
