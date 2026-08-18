import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveStackConfig } from './stack.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DOWN = path.resolve(__dirname, '../../../sandbox/pivot/try/down.ps1');

/** Tear the isolated stack down and assert that it did not mutate the worktree. */
export default function globalTeardown() {
  const stack = resolveStackConfig();
  // eslint-disable-next-line no-console
  console.log(`[brains-e2e] tearing down try-stack via ${DOWN}`);
  execFileSync(
    'powershell',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      DOWN,
      '-Port',
      String(stack.port),
      '-Name',
      stack.name,
    ],
    { stdio: 'inherit' },
  );
}
