# Brains Quality Gates

These gates define reproducible checks for the supported public product. They record
commands and healthy outcomes, not dated observations or candidate-specific proof.

## Fast checks

Run the smallest relevant tests while developing, then run:

```console
python scripts/check_docs.py
python scripts/check_traceability.py
```

A healthy result exits zero and reports no documentation or traceability drift. The full
repository gate owns generated core-surface validation with a fresh distribution build.

## Full repository gate

```console
python scripts/run_quality_gates.py
```

The runner owns the authoritative CI order. A healthy result means formatting, static
analysis, unit and integration tests, frontend checks, distribution checks, and the
generated core-surface manifest all pass. Do not copy a dated pass count into docs.

## Isolation

Tests that can alter a service, database, state directory, client configuration, or
listener must use disposable synthetic state. Run those tests in an isolated container
or disposable VM. Never mount an operator's Brains state or client configuration and
never bind the default Brains host ports.

Network access may be enabled while fetching build dependencies. The test runtime should
use no host mounts or published ports unless the test validates a synthetic boundary.

## Native lifecycle probes

The required installation probe validates exact-wheel identity, native-definition
rendering, and reversible harness wiring without mutating a service manager. The
isolated Claude probe validates generated hook discovery and continuation. Neither
substitutes for required native service-manager, atomic-exchange, or owner-permission
evidence, or establishes reboot persistence.

Candidate qualification must bind the source, wheel, sdist, and OCI image manifest
digests in one fail-closed result. Container runtime smoke must execute against that
exact OCI image. Publication rebuilds the wheel and sdist from the tagged source rather
than reusing the qualified files, so the binding that publication preserves is the
source commit, not the artifact digests. Treat a mismatched or missing source binding as
invalidating; reusing qualified artifacts end to end remains unimplemented.

Platform service-manager behavior cannot be established by a Linux container. The
guarded `scripts/probe_native_service_lifecycle.py` probe must run on a disposable native
account for each supported operating system. It exercises the real installer and service
manager, binds the installed artifact to the candidate package manifest, and emits
machine-readable preparation, verification, and cleanup records.

The real service-manager cycle and cleanup are required; actual reboot testing is
optional. Use `manager-cycle` for the native manager path, then `cleanup` to restore the
captured pre-test state. A cycle or preparation record is not login or reboot evidence.
If reboot testing is performed, use `prepare`, an actual reboot, `verify`, and `cleanup`;
the reboot check remains incomplete until post-reboot verification is bound to the same
journey and machine-observed boot transition. Cleanup is required even when the optional
reboot check is skipped or fails, and must fail closed when restoration cannot be proven.

The optional reboot chain's cleanup reader accepts phase-specific preparation and
verification records. A verification record must carry a valid `prepare_record_sha256`
matching its preparation record; missing, malformed, mismatched or extra fields are
rejected. Verification leaves the sealed preparation plan and its step list immutable.
Synthetic tests exercise serialized records through cleanup; they do not establish
native reboot persistence.

The lifecycle probe checks Task Scheduler XML/principal identity, systemd loaded-unit
properties, or launchd loaded-job fields against the exact dry-run definition and local
file. Only recognized not-found results establish absence; manager failures, foreign
definitions, and linked paths fail closed. An unloaded macOS plist remains an installed
definition, not an absent resource. In-process rollback uses only context acquired after
guards and provenance; an unsealed plan never authorizes cross-process recovery. Native
teardown is independent of configuration cleanup, which preserves drifted or linked paths.
Configuration inventory includes Claude's exact timestamped backup siblings and the
OpenCode plugin directory. Cleanup preserves preexisting backups and removes only recorded
created directories, bottom-up while empty. Native stop is polled for bounded quiescence
with definition ownership rechecked on every poll, including after launchd unloads a job.
After shutdown, a journey/root ownership marker and bounded runtime inventory authorize
removal. Known database/log bytes may change; unexpected files, directories, links, or
resources are retained and fail cleanup. This is a disposable single-writer probe, not
an atomic fence against hostile concurrent replacement. Abrupt process death before
sealing still requires operator review; it has no automatic cross-process rollback.

Native execution also requires the service manager's launch environment to select the
same private state. Windows task XML persists `BRAINS_STATE_DIR` in its bootstrap before
importing Brains; previously installed tasks require reinstallation. An explicit
`BRAINS_DB_URL` remains a separate override. Mocked bootstrap and cleanup tests do not
substitute for native launch-state qualification.

On Windows, the task-owned runner must recover a killed recorded supervisor tree after
its 60-second backoff, with a new verified supervisor PID and restored protocol readiness.
This recovery occurs within the running task action; a changed Scheduler `LastRunTime`
or a new task-run event is not required and must not be claimed without observation.
The unchanged manager-cycle probe's supervisor-kill and readiness checks remain required.
Stop must end the task-owned runner before captured-child cleanup and leave no respawn,
listener, or launcher/redirector process behind. The Windows probe first permits a bounded
30-second stop transition, then requires 65 seconds of continuously sampled quiescence,
longer than the runner's 60-second backoff. Every sample rechecks native ownership,
absent supervisor PID/listeners, and Scheduler Ready/Disabled state with zero instances.
Unknown/unavailable Scheduler state or any observed respawn fails qualification. This is
a finite no-respawn observation, not proof of permanent process containment or complete
ancestor disappearance. It does not separately exercise stopping during crash backoff.
Unit tests of the bounded 9999-restart
budget, clean exit, launch failure, cancellation, and state/argument binding are not a
substitute for this native evidence. Scheduler's configured action-failure retry policy
is separate and is not proof that Scheduler retried a failed supervisor.

## Recurring release conditions

Backlog completion does not qualify a particular candidate. Before a release, all of the
following must hold for the exact commit proposed for the tag:

1. Run the full repository gate from a clean checkout in Docker-isolated synthetic state.
   A healthy run exits zero, leaves no service, client configuration, database, or
   listener behind, and does not mount or address an operator's Brains installation.
2. Require the existing CI workflow to pass for that same commit. Its native installation
   matrix installs the candidate wheel on Windows, macOS, and Linux for both supported
   Python versions and every supported adapter, then verifies reversible wiring evidence.
   This condition is machine-enforced: the release workflow's `qualify` job refuses to
   publish unless the aggregate `quality gate` check succeeded for the tagged commit.
3. Require the existing Windows and macOS Claude recovery jobs for that commit to pass
   the native atomic-exchange, owner-permission, abrupt-interruption, exact-restoration,
   and pinned-Claude discovery/continuation probes.
4. On disposable Windows, macOS, and Linux accounts, run the guarded native service
    probe through the real `manager-cycle` and `cleanup`. Bind every record to the same
    candidate/package provenance and reject failed manager recovery, a surviving listener
    or definition, or unproven configuration restoration. Actual reboot testing is
    optional, not a release blocker. If performed, bind its `prepare`, post-reboot `verify`,
    and `cleanup` records to the same journey and observed boot transition; do not report
    a skipped or incomplete reboot check as passed.
5. A human reviews the candidate identity and all required evidence before approving any
   merge to `main`, remote push, tag, or publication. The tag-triggered release workflow
   verifies that the tag matches the package version, refuses to publish without a green
   `quality gate` for that exact commit, rebuilds from the tagged source, and publishes
   only through its configured protected environment. A passing earlier commit or a
   Docker-only substitute does not qualify the tag.

Both PyPI and GHCR publication jobs use the `pypi` environment; GitHub Release creation
waits for both. Before tagging, inspect that environment's required reviewers, self-review
prevention and administrator-bypass settings. Environment protection is repository
configuration, not enforced by the YAML alone. With self-review prevention enabled, a
workflow initiator cannot approve their own deployment; a sole reviewer who also initiates
the run blocks publication. Sharing an approval environment does not make the two uploads
atomic, and already-published packages cannot be rolled back by rejecting the other job.

Conditions 1, 3, and 4 are human-run and are not enforced by the release workflow.

These are recurring release conditions, not backlog items or a dated evidence diary.

## Acceptance

Acceptance tests must prove the linked issue's acceptance criteria and observable user
outcomes. Existing `F*`, `B*`, `J*`, `O*`, and `AC-*` test identifiers remain useful
references, not a requirement to maintain a separate feature registry. Passing lower-level
tests does not replace an end-to-end acceptance check when a change crosses CLI, MCP,
browser, persistence, or service boundaries.

## Documentation

Public documentation states current supported behavior, target contracts, and unresolved
evidence gaps separately. Record a command and its healthy result instead of a manual
verification timestamp, commit hash, test count, screenshot pack, or delivery diary.

[GitHub Issues](https://github.com/xibodev/brains-ai/issues) hold outcomes, acceptance
criteria, and delivery evidence. Keep priority, order, and status only in the
[Brains Project](https://github.com/orgs/xibodev/projects/1), not in Markdown backlog
documents or external shadow copies.
