import { defineConfig, devices } from '@playwright/test';

/**
 * Brains E2E config for the isolated console harness.
 *
 * The stack is started by `sandbox/pivot/try/up.ps1` (isolated Brains hub on :8810,
 * never touches ~/.brains or :8787). Point the suite at it with:
 *
 *   BRAINS_E2E_BASE_URL  (default http://127.0.0.1:8810)
 *   BRAINS_E2E_KEY       (default "try-brains" — the up.ps1 sign-in key)
 *
 * Set BRAINS_E2E_AUTO_STACK=1 to have globalSetup boot/teardown the stack for
 * you (Windows/PowerShell only). In CI the workflow boots the stack explicitly.
 */
const BASE_URL = process.env.BRAINS_E2E_BASE_URL ?? 'http://127.0.0.1:8810';

export default defineConfig({
  testDir: './specs',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  globalSetup: process.env.BRAINS_E2E_AUTO_STACK ? './fixtures/global-setup.ts' : undefined,
  globalTeardown: process.env.BRAINS_E2E_AUTO_STACK ? './fixtures/global-teardown.ts' : undefined,
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
