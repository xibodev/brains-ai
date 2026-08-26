import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J6 — Create, assign, and dispatch an Issue.
 *
 * Authority: F4, AC-F4-01 through AC-F4-07, and AC-F3-03. These deterministic
 * checks cover comments, assignment, and dispatch against seeded Brains state.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J6 (F3.3) a comment posted on an issue appears on the issue', async ({ page, consoleGuard }) => {
  await page.goto('/app/labs/issues');
  // Open the first issue card.
  await page.locator('.issue-card').first().click();

  const body = `e2e note ${Date.now()}`;
  await page.getByPlaceholder(/add a comment/i).fill(body);
  await page.getByRole('button', { name: /^comment$/i }).click();

  const comments = page.locator('[data-testid="issue-comments"]');
  await expect(comments.getByText(body)).toBeVisible();
  consoleGuard.assertClean();
});

test('J6 (F4) assign an issue to a persona and dispatch it', async ({ page }) => {
  await page.goto('/app/labs/issues');
  await page.locator('.issue-card').first().click();

  // Assign to the seeded bound persona 'Mason' via the tri-modal picker.
  await page.getByLabel(/assign/i).selectOption({ label: 'Mason' });
  // Dispatch spawns a session for the assigned persona.
  const [resp] = await Promise.all([
    page.waitForResponse((r) => /\/dispatch$/.test(r.url())),
    page.getByRole('button', { name: /dispatch/i }).click(),
  ]);
  expect(resp.ok(), `dispatch failed: ${resp.status()}`).toBeTruthy();

  await page.goto('/app/labs/sessions');
  await expect(page.getByText(/no sessions yet/i)).toHaveCount(0);
});
