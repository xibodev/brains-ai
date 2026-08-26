import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveStackConfig } from './stack.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '../../..');

function stateDir(): string {
  if (process.env.BRAINS_E2E_STATE_DIR) return process.env.BRAINS_E2E_STATE_DIR;
  if (process.env.RUNNER_TEMP) return path.join(process.env.RUNNER_TEMP, 'brains-e2e-state');
  return path.join(os.tmpdir(), 'brains-e2e', resolveStackConfig().name);
}

function runSeed(script: string): Record<string, unknown> {
  const container = process.env.BRAINS_E2E_SEED_CONTAINER;
  if (container) {
    const output = execFileSync('docker', ['exec', container, 'python', '-c', script], {
      cwd: ROOT,
      encoding: 'utf8',
    });
    return JSON.parse(output.trim()) as Record<string, unknown>;
  }

  const state = stateDir();
  mkdirSync(state, { recursive: true });
  const configPath = path.join(state, 'brains.yaml');
  if (!existsSync(configPath)) writeFileSync(configPath, '{}\n', 'utf8');
  const dbPath = path.join(state, 'brains.db').replaceAll('\\', '/');
  const output = execFileSync('uv', ['run', 'python', '-c', script], {
    cwd: ROOT,
    encoding: 'utf8',
    env: {
      ...process.env,
      PYTHONPATH: path.join(ROOT, 'src'),
      BRAINS_STATE_DIR: state,
      BRAINS_DB_URL: `sqlite:///${dbPath}`,
      BRAINS_CONFIG: configPath,
      BRAINS_RUNTIME_OVERLAY: path.join(state, 'brains.runtime.yaml'),
      BRAINS_PREWARM_INDEX_ON_SESSION: '0',
    },
  });
  return JSON.parse(output.trim()) as Record<string, unknown>;
}

export function seedWorkspace(): Record<string, unknown> {
  const state = (
    process.env.BRAINS_E2E_SEED_STATE_DIR ?? stateDir()
  ).replaceAll('\\', '/');
  return runSeed(`
import json
from brains.control.orgs import get_org
from brains.control.sessions import register_workspace
org = get_org("demo")
workspace = register_workspace(
    "${state}/workspace",
    slug="e2e-workspace",
    name="E2E Workspace",
    org_id=int(org["id"]),
)
print(json.dumps({"id": workspace.id, "slug": workspace.slug, "path": workspace.path}))
`);
}

export function seedApproval(): Record<string, unknown> {
  seedWorkspace();
  const state = stateDir().replaceAll('\\', '/');
  return runSeed(`
import json
from brains.control.decisions import file_decision_request
result = file_decision_request(
    "${state}/workspace",
    title="[gate] approve sealed E2E action",
    body="This is a local, simulated approval used to prove the browser governance loop.",
    proposed_answer="approve",
    metadata={"kind": "action_gate"},
)
print(json.dumps(result))
`);
}
