import { test, expect, signIn } from '../fixtures/console.js';
import type { Page } from '@playwright/test';

/**
 * J3 — Create and bind a Persona.
 *
 * Authority: F2 and AC-F2-01 through AC-F2-06. Backend evidence for capability
 * choices, binding persistence, and archive behavior lives in
 * tests/test_acceptance_brains.py; this spec covers the deterministic UI path.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

async function selectSimulatedRuntime(page: Page) {
  const response = await page.request.get('/v1/runtimes');
  if (!response.ok()) {
    throw new Error(`Runtime lookup failed: ${response.status()}`);
  }
  const body: {
    runtimes?: Array<{
      id?: number;
      status?: string;
      capabilities?: { models?: string[] };
    }>;
  } = await response.json();
  const runtime = body.runtimes?.find(
    (candidate) =>
      candidate.status === 'online' && (candidate.capabilities?.models?.length ?? 0) > 0,
  );
  if (!runtime?.id) {
    throw new Error('No online Runtime with model capabilities is available');
  }
  await page.getByLabel(/^runtime/i).selectOption(String(runtime.id));
}

test('J3.1 (F2.1) New-persona form derives the Model dropdown from the chosen runtime', async ({ page }) => {
  await page.goto('/app/personas');
  await page.getByRole('button', { name: /new persona|\+ new/i }).first().click();

  // Selecting a runtime must populate a real Model <select> (not a free-text input).
  await selectSimulatedRuntime(page);
  const modelSelect = page.getByLabel(/^model/i);
  await expect(modelSelect).toBeVisible();
  const options = await modelSelect.locator('option').count();
  expect(options).toBeGreaterThan(1);
});

test('J3.2 (F2.4) A persona can be deleted/archived from the UI', async ({ page }) => {
  // Self-contained: create a throwaway persona via the cascade, then delete it
  // (so the test is repeatable and never depletes seeded data).
  const name = `ephemeral-${Date.now()}`;
  await page.goto('/app/personas');
  await page.getByRole('button', { name: /new persona|\+ new/i }).first().click();
  await page.getByLabel(/^name/i).fill(name);
  await selectSimulatedRuntime(page);
  await page.getByLabel(/^model/i).selectOption({ index: 1 });
  await page.getByRole('button', { name: /^create$/i }).click();

  // Open the new persona and delete it.
  await expect(page.getByText(name).first()).toBeVisible();
  await page.getByText(name).first().click();
  // Wait for the detail drawer (its Save button) before targeting Delete exactly
  // (the persona cards are role=button too, so match the button name exactly).
  await expect(page.getByRole('button', { name: 'Save' })).toBeVisible();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();

  await expect(page.getByText(/archived|deleted/i)).toBeVisible();
  await expect(page.getByText(name)).toHaveCount(0);
});
