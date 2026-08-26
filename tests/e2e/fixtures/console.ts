import { test as base, expect, type Page } from '@playwright/test';

/**
 * Shared Brains E2E fixtures.
 *
 * `consoleGuard` supplies deterministic J11 evidence for AC-F0-03 and the
 * cross-cutting error/hygiene expectations: every visited screen must produce
 * zero console errors and zero failed `/v1` fetches.
 */

const KEY = process.env.BRAINS_E2E_KEY ?? 'try-brains';
const ORG = process.env.BRAINS_E2E_ORG ?? 'demo';

export type ConsoleGuard = {
  errors: string[];
  failedRequests: string[];
  assertClean: () => void;
};

function attachConsoleGuard(page: Page): ConsoleGuard {
  const errors: string[] = [];
  const failedRequests: string[] = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('requestfailed', (req) => {
    failedRequests.push(`${req.method()} ${req.url()} — ${req.failure()?.errorText}`);
  });
  page.on('response', (resp) => {
    // Surface 4xx/5xx on the app's own API as a failed fetch.
    if (resp.status() >= 400 && resp.url().includes('/v1/')) {
      failedRequests.push(`${resp.status()} ${resp.request().method()} ${resp.url()}`);
    }
  });

  return {
    errors,
    failedRequests,
    assertClean() {
      expect(errors, `console errors:\n${errors.join('\n')}`).toEqual([]);
      expect(failedRequests, `failed fetches:\n${failedRequests.join('\n')}`).toEqual([]);
    },
  };
}

/** Sign into the console with the try-stack admin key (idempotent). */
export async function signIn(page: Page): Promise<void> {
  // Pin the active org to the seeded one BEFORE first paint, so screens that
  // assert on seeded data (personas, issues) don't land on an empty default org.
  await page.addInitScript((org) => {
    try {
      window.localStorage.setItem('brains.activeOrg', org as string);
    } catch {
      /* ignore */
    }
  }, ORG);
  await page.goto('/app');
  const keyField = page.locator('input[type="password"], input[name="key"], input[placeholder*="key" i]').first();
  if (await keyField.count()) {
    await keyField.fill(KEY).catch(() => {});
    await page
      .getByRole('button', { name: /sign in|continue|enter/i })
      .first()
      .click()
      .catch(() => {});
    await page.waitForLoadState('networkidle').catch(() => {});
  }

  // The isolated harness represents a live daemon with a simulated Runtime.
  // Refresh its heartbeat before each journey so the production stale-runtime
  // sweep remains enabled without turning the fixture offline mid-suite.
  const response = await page.request.get('/v1/runtimes');
  if (response.ok()) {
    const body = (await response.json()) as { runtimes?: Array<{
      id?: number;
      machine_id?: string;
      capabilities?: { models?: string[] };
    }> };
    for (const runtime of body.runtimes ?? []) {
      if (!runtime.id || !runtime.machine_id || !runtime.capabilities?.models?.length) continue;
      await page.request.post('/v1/runtimes/heartbeat', {
        data: {
          machine_id: runtime.machine_id,
          runtimes: [{ id: runtime.id, status: 'online', health: 'healthy' }],
        },
      });
    }
  }
}

export const test = base.extend<{ consoleGuard: ConsoleGuard }>({
  consoleGuard: async ({ page }, use) => {
    const guard = attachConsoleGuard(page);
    await use(guard);
  },
});

export { expect };
