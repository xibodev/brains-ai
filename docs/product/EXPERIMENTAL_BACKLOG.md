<!--
last_verified: 2026-08-30T22:45:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD eedab318896d87fa9520f92736e42445383b2c6f plus mailbox-outcome analytics candidate inspection and isolated Docker suppression, censoring, lifecycle, privacy, and packaged browser evidence; real field outcomes and deployment not verified
-->

# Brains Experimental Feature Backlog

## Definition

An active experiment is implemented and intentionally available to a bounded audience,
but its behavior in real use is not fully understood. It must have independent
activation, truthful readiness/failure behavior, privacy-safe observation, a feedback
path, and operator-visible disable/rollback. Usage alone does not prove value.

Known-faulty frozen implementations do not belong here. Replacement research does not
create an experiment; a separately reviewed implementation must first be admitted as a
field trial.

## Agent Feedback Inbox

**Owner:** BL-P1-15.

**Feature mapping:** F3, B2, B4, B8; J7, J8, J11.
**Hypothesis:** Agents will report actionable friction when reporting is Workspace-scoped,
privacy-safe, deduplicated, and cannot alter roadmap or authority without human triage.

**Implemented trial:** Live Workspace Sessions can report and enrich canonical records;
same-surface duplicates link; browser/local humans triage and promote exactly once into
a Task, knowledge entry, or existing backlog reference.

**Observation backlog:** Measure duplicate quality, missing evidence, redaction loss,
time-to-triage, promotion usefulness, discarded reports, cross-harness participation,
and whether feedback changes subsequent outcomes.

**Safety boundary:** Agents cannot approve roadmap, create public issues, authorize an
effect, or include secrets/customer data. Promotion remains human-only and
audit-correlated.

**Decision rule:** Continue while reports are privacy-safe and materially actionable;
revise matching/redaction when false grouping or evidence loss dominates; withdraw if
safe triage cannot be sustained.

**Evidence required:** E4 two-agent report/enrichment plus human triage, promotion,
discard, duplicate, and recovery; periodic privacy review over redacted samples.

## Adoption and Outcome Analytics

**Owner:** BL-P1-16.

**Feature mapping:** F3, B2, B8; J7, J8, J11.
**Hypothesis:** Privacy-safe feature outcomes can reveal defects and adoption barriers
without storing prompts, arguments, source, logs, or raw outputs.

**Implemented trial:** Operations reports welcome offers and nearby follow-up events
with eligible/right-censored, minimum-group-suppressed denominators. Durable-mail outcomes
separately aggregate address registration, local acceptance/refusal, adapter wakeup, read, reply, forward,
explicit broadcast, and SMTP copy over the same exact window. Each family uses
`success`, `empty`, `refused`, `timeout`, `failed`, and `uncertain`; unavailable results
remain zero rather than being silently omitted.

**Observation backlog:** Add canonical tool and raw-adapter identity, transport, feature
family, duration, semantic result (`success`, `empty`, `refused`, `timeout`, `failed`),
passive versus explicit consumption, exact timestamp windows, and workflow/user outcome
separation. Disabled or withdrawn features must have separate denominators and never
reduce normal-product adoption. Legacy Session-addressed mail and `state='running'`
presence data are not valid baselines for durable mailbox adoption. After BL-P1-12 and
BL-P1-14 land, measure address registration, accepted mail, wakeup, read, reply,
forward, explicit broadcast, rejection reason, and SMTP-copy outcome as separate local
events; no message subject, body, address path, native Session ID, or native mailbox
object ID enters analytics. A minimum group of three is required before a non-zero bucket
is shown; denominators and non-zero peer buckets are suppressed whenever subtraction
could reveal a smaller bucket. Generic event totals are allowlisted and cannot bypass a
suppressed session, welcome, or mailbox bucket.

**Safety boundary:** Record no prompt, argument, secret, source path, output, customer
data, or machine identity. Aggregation must enforce minimum group size before anything
leaves the local install.

**Decision rule:** Continue while reports predict reproducible product defects or
friction; revise metrics that reward empty calls or ceremony; withdraw if useful
analysis requires private content.

**Evidence required:** E3 CLI/MCP/SSE/stdio success, empty, timeout, refusal, alias,
exact-window, censoring, and privacy tests; E4 multi-harness reconciliation without
reading harness-private transcripts.

## Ephemeral Peer Review Admission Blocker

**Owner:** BL-P1-20.

**Feature mapping:** F3, B2, B4, B8; J7, J8, J11.
**Lifecycle:** Implemented candidate, not an active experiment. MCP and CLI currently
default exact-tool Workspace help to `execution_mode=auto`, so ordinary peer-help calls
can launch it without independent opt-in. It cannot be field-observed as an admitted
experiment until the normal default is `existing`, explicit `auto`/`ephemeral`
activation is independently gated and disableable, and the worker transport no longer
depends on withdrawn Runtime activation.

**Hypothesis:** A fresh exact-tool reviewer operating on a disposable tracked snapshot
can return useful evidence when no eligible live peer responds, without modifying the
registered source.

**Implemented trial:** Exact-tool help requests can use `auto` or `ephemeral`; one fenced
execution receives a temporary tracked snapshot, bounded prompt/output/runtime, and
provider-specific no-write policy; source fingerprints are checked before accepting an
answer; attempts are leased and attributed to a terminal Session.

**Observation backlog:** Measure launch success by harness/host, time to first answer,
answer acceptance, false and duplicate findings, source-fingerprint rejection, retry
recovery, cost, and whether live peers should receive a longer claim window.

**Safety boundary:** Never pass the registered source path, never accept changed-source
answers, never claim universal child network confinement, and never merge or execute a
review finding automatically.

**Decision rule:** Continue when reviews are timely and materially useful without source
or credential leakage; tune routing and limits from evidence; withdraw automatic launch
if isolation or answer quality cannot be trusted.

**Evidence required:** First prove default existing-peer behavior and a bounded explicit
experiment gate. Then run controlled real-provider E4 across supported local harness
transports, including unavailable CLI, timeout, mutation attempt, changed source,
malformed answer, retry, and cleanup. A separately admitted remote worker transport may
be tested only after its boundary is distinct from withdrawn Runtime enrollment and
execution.

## Admission Template

A future implementation enters this backlog only after an explicit decision records:

- advertised hypothesis and audience;
- independent installation and activation;
- readiness, disable, rollback, and removal contracts;
- privacy-safe usage/defect telemetry;
- owner and review point;
- success and stop criteria;
- stable feature, journey, acceptance, and backlog mappings.

The community defect relay (BL-P1-19) is active implementation work, not yet an
experiment. The isolated capability incubator is research, not an experiment.
