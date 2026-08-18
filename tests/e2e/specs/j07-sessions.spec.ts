import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J7 — Dispatch and watch a Session.
 *
 * Authority: F3, AC-F3-01/02, AC-F4-03/04, AC-F1-06, and AC-F0-04.
 * tests/test_acceptance_brains.py covers the backend Spawn and event contracts;
 * this deterministic spec covers the browser Session, transcript, and Inbox
 * surfaces without claiming a real-daemon E4 run.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J7.1 (F0.1+F2) Spawning a bound persona creates a session that appears in Sessions', async ({ page, consoleGuard }) => {
  await page.goto('/app/personas');
  // 'mason' is seeded bound to an online runtime, so its Spawn produces a session.
  const masonCard = page.locator('[data-testid="persona-card"]', { hasText: /mason/i }).first();
  // Wait for the spawn POST to complete before navigating (else it aborts).
  const [resp] = await Promise.all([
    page.waitForResponse((r) => /\/personas\/.*\/spawn/.test(r.url())),
    masonCard.getByRole('button', { name: /spawn/i }).click(),
  ]);
  expect(resp.ok(), `spawn failed: ${resp.status()}`).toBeTruthy();

  await page.goto('/app/sessions');
  await expect(page.getByText(/no sessions yet/i)).toHaveCount(0);
  consoleGuard.assertClean();
});

test('J7.2 (F3.1) Session detail shows the streamed transcript', async ({ page }) => {
  await page.goto('/app/personas');
  const masonCard = page.locator('[data-testid="persona-card"]', { hasText: /mason/i }).first();
  const [resp] = await Promise.all([
    page.waitForResponse((r) => /\/personas\/.*\/spawn/.test(r.url())),
    masonCard.getByRole('button', { name: /spawn/i }).click(),
  ]);
  const spawn = await resp.json();
  const sessionId = spawn.session_id;
  const runtimeId = spawn.runtime_id;
  expect(sessionId, 'spawn returned a session id').toBeTruthy();

  // Simulate the daemon streaming a stdout chunk (what a real runtime posts).
  const marker = `STREAM-${Date.now()}`;
  const ev = await page.request.post(
    `/v1/runtimes/${runtimeId}/sessions/${sessionId}/events`,
    { data: { seq: 1, stream: 'stdout', chunk: marker } },
  );
  expect(ev.ok(), `event ingest failed: ${ev.status()}`).toBeTruthy();

  // Open the session detail and assert the streamed chunk renders in the transcript.
  await page.goto('/app/sessions');
  await page.locator('.card-list .softcard').first().click();
  await expect(page.getByText(marker)).toBeVisible({ timeout: 7000 });
});

test('J7.3 (F3.4) The Inbox surfaces the gate approvals queue', async ({ page, consoleGuard }) => {
  // The gate files into the same store the Inbox Approvals tab reads; the full
  // intercept -> queue -> resolve loop is guarded by the F3.4 acceptance tests
  // (backend) + tests/test_gate_integration.py (real spawn on Linux). Here we
  // assert the operator surface renders + is interactive, console-clean.
  await page.goto('/app/inbox');
  const approvalsTab = page
    .getByRole('tab', { name: /approvals/i })
    .or(page.getByRole('button', { name: /approvals/i }))
    .first();
  await expect(approvalsTab).toBeVisible();
  await approvalsTab.click();
  consoleGuard.assertClean();
});
