import { test, expect, signIn } from '../fixtures/console.js';
import { seedApproval, seedMailboxJourney } from '../fixtures/seed.js';

/**
 * J8 — Ask, approve, steer, chat, and stop.
 *
 * Authority: F3, AC-F3-04 through AC-F3-07, and AC-B4-01 through AC-B4-04.
 * Browser coverage is limited to advertised governance and fail-closed behavior
 * for withdrawn execution controls.
 */

let approvalCode = '';
let mailboxJourney: Record<string, unknown> = {};

test.beforeAll(() => {
  approvalCode = String(seedApproval().code);
  mailboxJourney = seedMailboxJourney();
});

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('J8.1 a pending governed action is approved once from Governance', async ({ page, consoleGuard }) => {
  await page.goto('/app/governance');
  const card = page.locator('.operator-decision-card', { hasText: approvalCode });
  await expect(card).toBeVisible();
  const [response] = await Promise.all([
    page.waitForResponse((resp) => resp.url().endsWith(`/approvals/${approvalCode}/resolve`)),
    card.getByRole('button', { name: /approve/i }).click(),
  ]);
  expect(response.ok(), `approval failed: ${response.status()}`).toBeTruthy();
  await expect(card).toHaveCount(0);

  const resolved = await page.request.get(`/v1/approvals/${approvalCode}`);
  expect(resolved.ok()).toBeTruthy();
  expect((await resolved.json()).status).toBe('resolved');
  consoleGuard.assertClean();
});

test('J8.2 withdrawn execution-control routes fail closed while governance remains available', async ({ page, consoleGuard }) => {
  for (const route of [
    '/app/sessions',
    '/app/sessions/session-1',
    '/app/labs/sessions',
    '/app/labs/sessions/session-1',
  ]) {
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page).toHaveURL(/\/app\/command-center$/);
    await expect(page.getByRole('heading', { name: 'Command Center' })).toBeVisible();
  }

  await page.goto('/app/governance');
  await expect(page.getByRole('heading', { name: /governance/i })).toBeVisible();
  await expect(page.getByRole('button', { name: /verify audit chain/i })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Labs' })).toHaveCount(0);
  consoleGuard.assertClean();
});

test('J8.3 durable operator mail is explicit, threaded, and recoverable', async ({ page, consoleGuard }) => {
  await page.goto('/app/coordination');
  const desk = page.locator('.operator-mailroom');
  await expect(desk.getByRole('heading', { name: 'Mailbox desk' })).toBeVisible();
  await expect(desk.getByText(String(mailboxJourney.subject), { exact: true })).toBeVisible();

  await desk.getByText(String(mailboxJourney.subject), { exact: true }).click();
  const thread = desk.locator('.operator-mail-thread');
  await expect(thread.locator('h3')).toHaveText(String(mailboxJourney.subject));
  await expect(thread.getByText('The durable context survived while the operator was away.')).toBeVisible();
  await expect(thread.locator('.operator-delivery-states > span.accepted')).toContainText('Accepted');

  const [read] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes(`/v1/operator/mailboxes/threads/${mailboxJourney.thread_id}/read`) &&
      response.request().method() === 'POST',
    ),
    thread.getByRole('button', { name: 'Mark thread read' }).click(),
  ]);
  expect(read.ok(), `mark read failed: ${read.status()}`).toBeTruthy();
  await expect(thread.locator('.operator-delivery-states > span.read')).toContainText('Read');

  await thread.getByRole('button', { name: 'Reply' }).click();
  const composer = desk.locator('.operator-mail-composer');
  await expect(composer.getByRole('heading', { name: 'Reply in thread' })).toBeVisible();
  await expect(composer.getByText(String(mailboxJourney.sender_address))).toBeVisible();
  await composer.getByLabel('Message').fill('Acknowledged from the durable operator inbox.');
  const [reply] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes(`/v1/operator/workspaces/${mailboxJourney.workspace}/mailboxes/messages/${mailboxJourney.message_id}/reply`) &&
      response.request().method() === 'POST',
    ),
    composer.getByRole('button', { name: 'Send mail' }).click(),
  ]);
  expect(reply.ok(), `reply failed: ${reply.status()}`).toBeTruthy();
  const replied = desk.locator('.operator-mail-timeline article', { hasText: 'Acknowledged from the durable operator inbox.' });
  await expect(replied.getByText('Acknowledged from the durable operator inbox.', { exact: true })).toBeVisible();
  await expect(replied.locator('.operator-delivery-states > span.accepted')).toContainText('Accepted');
  await replied.getByRole('button', { name: 'Forward' }).click();
  const forwarder = desk.locator('.operator-mail-composer');
  await expect(forwarder.getByRole('heading', { name: 'Forward with provenance' })).toBeVisible();
  await forwarder.getByLabel(String(mailboxJourney.other_address)).check();
  await forwarder.getByLabel('Message').fill('Forwarded for a second agent to inspect.');
  const [forward] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes(`/v1/operator/workspaces/${mailboxJourney.workspace}/mailboxes/messages/`) &&
      response.url().endsWith('/forward') &&
      response.request().method() === 'POST',
    ),
    forwarder.getByRole('button', { name: 'Send mail' }).click(),
  ]);
  expect(forward.ok(), `forward failed: ${forward.status()}`).toBeTruthy();
  const forwarded = desk.locator('.operator-mail-timeline article', { hasText: 'Forwarded for a second agent to inspect.' });
  await expect(forwarded.getByText('Forwarded from operator:admin@brains')).toBeVisible();
  await expect(forwarded.getByText('Forwarded for a second agent to inspect.', { exact: true })).toBeVisible();

  await page.reload();
  const reloaded = page.locator('.operator-mailroom .operator-mail-timeline article', {
    hasText: 'Forwarded for a second agent to inspect.',
  });
  await expect(reloaded.getByText('Forwarded for a second agent to inspect.', { exact: true })).toBeVisible();
  consoleGuard.assertClean();
});

test('J8.4 mailbox desk is keyboard reachable and responsive', async ({ page, consoleGuard }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/app/coordination?mailbox=unknown%3Amailbox%40private');
  await expect(page.locator('.operator-mailroom').getByRole('alert')).toContainText('Mailbox unavailable');
  await expect(page.locator('.operator-mailroom').getByRole('button', { name: 'Compose mail' })).toHaveCount(0);

  await page.goto(`/app/coordination?mailbox=${encodeURIComponent(String(mailboxJourney.sender_address))}`);
  const desk = page.locator('.operator-mailroom');
  await expect(desk.getByLabel('Open mailbox')).toBeVisible();
  await expect(desk.getByRole('button', { name: 'Compose mail' })).toBeDisabled();
  await expect(desk.getByRole('button', { name: 'Compose mail' })).toHaveAttribute('title', /binding proof/);
  await expect(desk.getByText('Read-only browser inspection. Agent sends require adapter proof.')).toBeHidden();
  const hasOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(hasOverflow).toBe(false);

  await desk.getByLabel('Open mailbox').selectOption('operator:admin@brains');
  await expect(desk.getByRole('button', { name: 'Compose mail' })).toBeEnabled();
  await desk.getByRole('button', { name: 'Compose mail' }).focus();
  await page.keyboard.press('Enter');
  await expect(desk.getByRole('heading', { name: 'Compose mail' })).toBeVisible();
  await expect(desk.getByLabel('Subject')).toBeFocused();
  consoleGuard.assertClean();
});

test('J8.5 compose uses the authorized address book and persists in Sent', async ({ page, consoleGuard }) => {
  const subject = `Browser compose ${Date.now()}`;
  await page.goto('/app/coordination');
  const desk = page.locator('.operator-mailroom');
  await desk.getByRole('button', { name: 'Compose mail' }).click();
  const composer = desk.locator('.operator-mail-composer');
  await expect(composer.getByRole('heading', { name: 'Compose mail' })).toBeVisible();
  await composer.getByLabel(String(mailboxJourney.other_address)).check();
  await composer.getByLabel('Subject').fill(subject);
  await composer.getByLabel('Message').fill('A new address-book message committed from the browser.');
  const [sent] = await Promise.all([
    page.waitForResponse((response) =>
      response.url().includes(`/v1/operator/workspaces/${mailboxJourney.workspace}/mailboxes/messages`) &&
      !response.url().endsWith('/reply') &&
      !response.url().endsWith('/forward') &&
      response.request().method() === 'POST',
    ),
    composer.getByRole('button', { name: 'Send mail' }).click(),
  ]);
  expect(sent.ok(), `compose failed: ${sent.status()}`).toBeTruthy();
  await expect(desk.getByRole('tab', { name: 'Sent' })).toHaveAttribute('aria-selected', 'true');
  await expect(desk.locator('.operator-mail-thread h3')).toHaveText(subject);
  await expect(desk.locator('.operator-delivery-states > span.accepted')).toContainText('Accepted');
  consoleGuard.assertClean();
});
