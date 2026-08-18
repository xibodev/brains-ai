import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J2 — Connect a machine and detect CLIs.
 *
 * Authority: F1 and AC-F1-01 through AC-F1-06. The deterministic browser
 * harness simulates token redemption with a fresh machine and CLI inventory;
 * it does not claim E4 evidence for a real Runtime daemon.
 */

import type { Page } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

async function openConnectModal(page: Page) {
  await page.goto('/app/runtimes');
  await page
    .getByRole('button', { name: /connect a machine|\+ connect|add (a )?machine/i })
    .first()
    .click();
}

test('J2.1 Runtimes screen offers a "Connect a machine" entry point', async ({ page, consoleGuard }) => {
  await page.goto('/app/runtimes');
  await expect(
    page.getByRole('button', { name: /connect a machine|\+ connect|add (a )?machine/i }).first(),
  ).toBeVisible();
  consoleGuard.assertClean();
});

test('J2.2 (F1.1) Connect modal shows a valid, complete one-line command', async ({ page }) => {
  await openConnectModal(page);
  const command = await page.locator('[data-testid="connect-command"], code, pre').first().innerText();

  expect(command).toContain('brains-ai');
  expect(command).not.toContain('<url>');
  expect(command).not.toContain('pip install brains ');
  expect(command).toMatch(/--enrol\s+\S+/);
});

test('J2.3 (F1.3) Modal flips from "waiting" to "connected" when a machine redeems', async ({ page, request }) => {
  await openConnectModal(page);
  await expect(page.getByText(/waiting for (this|your) (machine|computer)/i)).toBeVisible();

  // Read the minted token out of the connect command and simulate a real
  // machine redeeming it (what `brains-ai daemon start --enrol <token>` does).
  const command = await page.locator('[data-testid="connect-command"]').first().innerText();
  const token = command.match(/--enrol\s+(\S+)/)?.[1];
  expect(token, 'token present in connect command').toBeTruthy();

  const machineId = `e2e-box-${Date.now()}`;
  const resp = await request.post('/v1/runtimes/enrol/redeem', {
    data: {
      token,
      machine_id: machineId,
      clis: [
        { tool: 'copilot', version: '1.0.65' },
        { tool: 'claude', version: '2.0.1' },
      ],
    },
  });
  expect(resp.ok(), `redeem failed: ${resp.status()}`).toBeTruthy();

  // The modal polls /v1/runtimes and should flip within ~5s.
  await expect(page.getByText(/connected|CLIs? detected/i)).toBeVisible({ timeout: 7000 });
});
