<!--
last_verified: 2026-08-29T12:28:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 92ebf88d5942ec143931303ba3f00df3a151583d plus static inspection of implemented feedback, adoption, and ephemeral-review surfaces and the approved future durable-mailbox measurement boundary; real field outcomes remain under observation; deployment not verified
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
with eligible/right-censored denominators.

**Observation backlog:** Add canonical tool and raw-adapter identity, transport, feature
family, duration, semantic result (`success`, `empty`, `refused`, `timeout`, `failed`),
passive versus explicit consumption, exact timestamp windows, and workflow/user outcome
separation. Disabled or withdrawn features must have separate denominators and never
reduce normal-product adoption. Legacy Session-addressed mail and `state='running'`
presence data are not valid baselines for durable mailbox adoption. After BL-P1-12 and
BL-P1-14 land, measure address registration, accepted mail, wakeup, read, reply,
forward, explicit broadcast, rejection reason, and SMTP-copy outcome as separate local
events; no message subject, body, address path, or native Session ID enters analytics.

**Safety boundary:** Record no prompt, argument, secret, source path, output, customer
data, or machine identity. Aggregation must enforce minimum group size before anything
leaves the local install.

**Decision rule:** Continue while reports predict reproducible product defects or
friction; revise metrics that reward empty calls or ceremony; withdraw if useful
analysis requires private content.

**Evidence required:** E3 CLI/MCP/SSE/stdio success, empty, timeout, refusal, alias,
exact-window, censoring, and privacy tests; E4 multi-harness reconciliation without
reading harness-private transcripts.

## Ephemeral Peer Review

**Owner:** BL-P1-20.

**Feature mapping:** F3, B2, B4, B8; J7, J8, J11.
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

**Evidence required:** Controlled real-provider and remote-Runtime E4 across supported
harnesses, including unavailable CLI, timeout, mutation attempt, changed source,
malformed answer, retry, and cleanup.

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
