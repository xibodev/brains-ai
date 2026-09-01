<!--
last_verified: 2026-08-31T18:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 35ce5ff1b4a2eb8bce2777ca7e3cff4d7ceece99 plus the worktree experimental-label and ordinary-feedback correction, analytics removal, and isolated Docker UAT; installed-service recovery and deployment not verified
-->

# Brains Experimental Feature Backlog

## Definition

An experimental feature is implemented behavior whose usefulness, ergonomics, or edge
cases are not yet proven in normal use. The label is a truthful uncertainty marker, not
an analytics program, deployment state, audience, or permission to weaken UAT. Every
feature still needs automated contracts and isolated end-to-end UAT before release.

After release, users report ordinary feedback through the supported BL-P1-15 feedback
surface. Engineers reproduce that feedback and revise experimental or established
behavior as appropriate. Brains does not need embedded behavioral analytics, cohorts,
observation windows, or a special field-trial process for that engineering loop.

Known-faulty frozen implementations do not belong here. Replacement research does not
become experimental product behavior until its implementation, activation, UAT,
feedback, disable, rollback, and revision boundaries have been reviewed.

## Feedback and Evidence Boundary

The agent feedback inbox is an advertised normal-product capability owned by BL-P1-15,
not an experiment. BL-P1-16's adoption/outcome analytics interpretation is removed.
Operational readiness and bounded diagnostic events remain because they report current
service failures rather than infer behavior or product value. Historical events remain
intact for store compatibility.

## Ephemeral Peer Review Admission Blocker

**Owner:** BL-P1-20.

**Feature mapping:** F3, B2, B4, B8; J7, J8, J11.
**Lifecycle:** Implemented candidate, not a supported experimental feature. MCP and CLI currently
default exact-tool Workspace help to `execution_mode=auto`, so ordinary peer-help calls
can launch it without independent opt-in. It cannot be advertised as experimental
until the normal default is `existing`, explicit `auto`/`ephemeral`
activation is independently gated and disableable, and the worker transport no longer
depends on withdrawn Runtime activation.

**Hypothesis:** A fresh exact-tool reviewer operating on a disposable tracked snapshot
can return useful evidence when no eligible live peer responds, without modifying the
registered source.

**Implemented candidate:** Exact-tool help requests can use `auto` or `ephemeral`; one fenced
execution receives a temporary tracked snapshot, bounded prompt/output/runtime, and
provider-specific no-write policy; source fingerprints are checked before accepting an
answer; attempts are leased and attributed to a terminal Session.

**Review focus:** Validate launch success by harness/host, time to first answer, answer
quality, source-fingerprint rejection, retry recovery, cleanup, and cost through isolated
UAT and ordinary feedback after release.

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

## Label Template

A future implementation enters this backlog only after its product contract records:

- the uncertain behavior and user promise;
- normal installation or independent activation, whichever is truthful;
- readiness, disable, rollback, and removal contracts;
- an ordinary user-feedback path;
- owner and engineering review point;
- UAT evidence and revision/withdrawal criteria;
- stable feature, journey, acceptance, and backlog mappings.

The label itself never creates telemetry, starts a field trial, or substitutes for UAT.
