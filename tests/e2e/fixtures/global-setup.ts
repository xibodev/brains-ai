import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveStackConfig } from './stack.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const UP = path.resolve(__dirname, '../../../sandbox/pivot/try/up.ps1');

/**
 * Boot the isolated normal-install stack (Windows/PowerShell only).
 * The harness never launches a real agent CLI.
 */
export default function globalSetup() {
  const stack = resolveStackConfig();
  // eslint-disable-next-line no-console
  console.log(`[brains-e2e] booting normal-install try-stack at ${stack.baseUrl} via ${UP}`);
  execFileSync(
    'powershell',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      UP,
      '-Port',
      String(stack.port),
      '-Name',
      stack.name,
    ],
    {
      env: { ...process.env, BRAINS_E2E_STACK_KEY: stack.key },
      stdio: 'inherit',
    },
  );
}
