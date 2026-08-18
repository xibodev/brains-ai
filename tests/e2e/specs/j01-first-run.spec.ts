import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J1 — Sign in and complete first run.
 *
 * Authority: F0/F6 and AC-F0-01/02 plus AC-F6-01 through AC-F6-05.
 * The isolated harness has a simulated Runtime, so this drives the complete
 * durable onboarding flow without launching an external agent CLI.
 */

test('J1.1 invalid credentials remain signed out and allow retry', async ({ page }) => {
  await page.goto('/app');
  const keyField = page.locator('input[type="password"], input[name="key"]').first();
  await expect(keyField).toBeVisible();
  await keyField.fill('definitely-wrong');
  await page.getByRole('button', { name: /sign in|continue|enter/i }).first().click();
  await expect(keyField).toBeVisible();
  await expect(page).toHaveURL(/admin\/login|\/app/);
});

test('J1.2 onboarding persists real entities and completes with a session', async ({ page, consoleGuard }) => {
  await signIn(page);
  await page.goto('/app/onboarding');

  await page.getByRole('button', { name: /use it/i }).click();
  await expect(page.getByRole('heading', { name: /connect a machine/i, level: 1 })).toBeVisible();

  await page.getByRole('button', { name: /connect a machine/i }).click();
  const command = await page.locator('[data-testid="connect-command"]').innerText();
  const token = command.match(/--enrol\s+(\S+)/)?.[1];
  expect(token, 'onboarding enrol token').toBeTruthy();
  const redeem = await page.request.post('/v1/runtimes/enrol/redeem', {
    data: {
      token,
      machine_id: `onboarding-${Date.now()}`,
      clis: [{ tool: 'copilot', version: 'e2e' }],
    },
  });
  expect(redeem.ok(), `redeem failed: ${redeem.status()}`).toBeTruthy();

  await expect(page.getByRole('heading', { name: /create your first persona/i })).toBeVisible({
    timeout: 7000,
  });
  await page.locator('select').first().selectOption({ index: 1 });
  const personaName = `Onboarding ${Date.now()}`;
  await page.getByLabel(/persona name/i).fill(personaName);
  await page.getByRole('button', { name: /continue/i }).click();

  await expect(page.getByRole('heading', { name: /create a project/i })).toBeVisible();
  await page.getByLabel(/project name/i).fill(`First run ${Date.now()}`);
  await page.getByLabel(/issue title/i).fill('Prove the complete first run');
  await page.getByRole('button', { name: /continue/i }).click();

  await expect(page.getByRole('heading', { name: /dispatch to your persona/i })).toBeVisible();
  await page.getByRole('button', { name: /dispatch/i }).click();
  await expect(page.getByRole('heading', { name: /you're set up/i })).toBeVisible();
  await expect(page.locator('[data-testid="onboarding-steps"]')).toContainText('dispatch: done');
  consoleGuard.assertClean();
});
