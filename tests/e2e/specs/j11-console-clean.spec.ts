import { test, expect, signIn } from '../fixtures/console.js';

/**
 * J11 — Cross-cutting trust, realtime, errors, accessibility, and hygiene.
 *
 * Authority: J11, AC-F0-01 through AC-F0-05, AC-F3-01/07, AC-F9-03,
 * AC-B8-01 through AC-B8-04, and AC-B9-01 through AC-B9-03. The deterministic
 * sweep covers core routes, console/network hygiene, and active-Org persistence.
 */

const ROUTES = [
  '/app',
  '/app/runtimes',
  '/app/personas',
  '/app/projects',
  '/app/issues',
  '/app/sessions',
  '/app/inbox',
  '/app/config',
  '/app/settings',
];

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

for (const route of ROUTES) {
  test(`J11 console-clean on ${route}`, async ({ page, consoleGuard }) => {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    // Deep-loading any route must never get stuck on "No org" / "Loading…".
    await expect(page.getByText(/^\s*(no org|loading…?)\s*$/i)).toHaveCount(0);
    consoleGuard.assertClean();
  });
}

test('J11 (F0.2) active org persists across a full reload', async ({ page }) => {
  await page.goto('/app/issues');
  await page.waitForLoadState('networkidle').catch(() => {});
  const orgBefore = await page.locator('[data-testid="active-org"]').first().innerText();

  await page.reload();
  await page.waitForLoadState('networkidle').catch(() => {});
  const orgAfter = await page.locator('[data-testid="active-org"]').first().innerText();

  expect(orgAfter).toBe(orgBefore);
  await expect(page.getByText(/^\s*no org\s*$/i)).toHaveCount(0);
});
