import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J10 — Manage Org automation and Skills.
 *
 * Authority: F9/F10, AC-F10-01 through AC-F10-06, and the J10 governance
 * mappings. These deterministic checks create browser-visible definitions;
 * gate enforcement and Skill attachment remain separately traced gaps.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J10 (F10) create an autopilot', async ({ page, consoleGuard }) => {
  await page.goto('/app/automation');
  await page.getByRole('button', { name: /new autopilot|\+ new autopilot/i }).first().click();

  const name = `nightly-${Date.now()}`;
  await page.getByLabel(/^name/i).first().fill(name);
  await page.getByLabel(/title template|title/i).first().fill('Nightly triage');
  await page.getByRole('button', { name: /^create$/i }).click();

  await expect(page.locator('[data-testid="autopilots-list"]').getByText(name)).toBeVisible();
  consoleGuard.assertClean();
});

test('J10 (F10) create a skill', async ({ page, consoleGuard }) => {
  await page.goto('/app/automation');
  await page.getByRole('button', { name: /new skill|\+ new skill/i }).first().click();

  const name = `Skill ${Date.now()}`;
  await page.getByLabel(/^name/i).first().fill(name);
  await page.getByRole('button', { name: /^create$/i }).click();

  await expect(page.locator('[data-testid="skills-list"]').getByText(name)).toBeVisible();
  consoleGuard.assertClean();
});
