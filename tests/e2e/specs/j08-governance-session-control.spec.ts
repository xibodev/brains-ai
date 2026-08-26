import { test, expect, signIn } from '../fixtures/console.js';
import { seedApproval } from '../fixtures/seed.js';

/**
 * J8 — Approve governed work and control a running Session.
 *
 * Authority: F3, AC-F3-04 through AC-F3-07, and AC-B4-01 through AC-B4-04.
 * The simulated Copilot Runtime cannot accept interactive follow-up input, so
 * the browser must show that limitation truthfully while stop remains durable.
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

test('J8.2 unsupported chat is explicit and stop is durably idempotent', async ({ page }) => {
  await page.goto('/app/labs/personas');
  const mason = page.locator('[data-testid="persona-card"]', { hasText: /mason/i }).first();
  const [spawnResponse] = await Promise.all([
    page.waitForResponse((resp) => /\/personas\/.*\/spawn/.test(resp.url())),
    mason.getByRole('button', { name: /spawn/i }).click(),
  ]);
  const spawn = await spawnResponse.json();
  const sessionId = String(spawn.session_id);

  await page.goto('/app/labs/sessions');
  await page.locator('.card-list .softcard').first().click();
  await page.getByRole('button', { name: /message session/i }).click();
  await expect(page.getByText(/messaging is unavailable/i)).toBeVisible();
  await expect(page.getByPlaceholder(/this agent cannot receive messages/i)).toBeDisabled();

  const operationId = `stop-${Date.now()}`;
  const first = await page.request.post(`/v1/sessions/${sessionId}/stop`, {
    data: { operation_id: operationId },
  });
  const replay = await page.request.post(`/v1/sessions/${sessionId}/stop`, {
    data: { operation_id: operationId },
  });
  expect(first.ok()).toBeTruthy();
  expect(replay.ok()).toBeTruthy();
  const firstBody = await first.json();
  const replayBody = await replay.json();
  expect(replayBody.command_id).toBe(firstBody.command_id);
});
