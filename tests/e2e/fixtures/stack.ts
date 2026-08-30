const DEFAULT_BASE_URL = 'http://127.0.0.1:8810';
const DEFAULT_KEY = 'try-brains';
const DEFAULT_NAME = 'trystack';

export type StackConfig = {
  baseUrl: string;
  key: string;
  name: string;
  port: number;
};

export function resolveStackConfig(): StackConfig {
  const rawBaseUrl = process.env.BRAINS_E2E_BASE_URL ?? DEFAULT_BASE_URL;
  const url = new URL(rawBaseUrl);
  const autoStack = Boolean(process.env.BRAINS_E2E_AUTO_STACK);
  if (autoStack && (
    url.protocol !== 'http:' ||
    !['127.0.0.1', 'localhost'].includes(url.hostname) ||
    url.username ||
    url.password ||
    (url.pathname !== '/' && url.pathname !== '') ||
    url.search ||
    url.hash
  )) {
    throw new Error(
      'BRAINS_E2E_AUTO_STACK requires a plain loopback HTTP base URL without credentials, path, query, or fragment',
    );
  }

  const port = Number(url.port || '80');
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('BRAINS_E2E_BASE_URL must contain a valid TCP port');
  }

  const key = process.env.BRAINS_E2E_KEY ?? DEFAULT_KEY;
  if (!key.trim()) {
    throw new Error('BRAINS_E2E_KEY must not be empty');
  }

  const name = process.env.BRAINS_E2E_STACK_NAME ?? DEFAULT_NAME;
  if (!/^[a-z0-9][a-z0-9_-]{0,62}$/.test(name)) {
    throw new Error('BRAINS_E2E_STACK_NAME must be a lowercase slug');
  }

  if (!autoStack && (url.protocol !== 'http:' || url.username || url.password)) {
    throw new Error('BRAINS_E2E_BASE_URL must be a plain HTTP origin without credentials');
  }

  return { baseUrl: url.origin, key, name, port };
}
