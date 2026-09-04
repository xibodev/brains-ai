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
substitutes for native service-manager, reboot, atomic-exchange, or owner-permission
evidence.

Candidate qualification must bind the source, wheel, sdist, and OCI image manifest
digests in one fail-closed result. Container runtime smoke must execute against that
exact OCI image, and publication must reuse the qualified wheel, sdist, and image;
missing, rebuilt, or mismatched artifacts invalidate the result.

Platform service-manager behavior cannot be established by a Linux container. The
guarded `scripts/probe_native_service_lifecycle.py` probe must run on a disposable native
account for each supported operating system. It exercises the real installer and service
manager, binds the installed artifact to the candidate package manifest, and emits
machine-readable preparation, verification, and cleanup records.

Preparation is not completion. A reboot-dependent check remains incomplete until a
post-reboot verification record is bound to the same journey and machine-observed boot
transition. Cleanup must restore the captured pre-test state and fail closed when that
restoration cannot be proven.

## Recurring release conditions

Backlog completion does not qualify a particular candidate. Before a release, all of the
following must hold for the exact commit proposed for the tag:

1. Run the full repository gate from a clean checkout in Docker-isolated synthetic state.
   A healthy run exits zero, leaves no service, client configuration, database, or
   listener behind, and does not mount or address an operator's Brains installation.
2. Require the existing CI workflow to pass for that same commit. Its native installation
   matrix installs the candidate wheel on Windows, macOS, and Linux for both supported
   Python versions and every supported adapter, then verifies reversible wiring evidence.
3. Require the existing Windows and macOS Claude recovery jobs for that commit to pass
   the native atomic-exchange, owner-permission, abrupt-interruption, exact-restoration,
   and pinned-Claude discovery/continuation probes.
4. On disposable Windows, macOS, and Linux accounts, run the guarded native service
   probe through `prepare`, an actual login or machine-observed reboot, `verify`, and
   `cleanup`. Bind every record to the same candidate/package provenance and reject a
   missing boundary, failed manager recovery, surviving listener or definition, or
   unproven configuration restoration.
5. A human reviews the candidate identity and all required evidence before approving any
   merge to `main`, remote push, tag, or publication. The existing tag-triggered release
   workflow verifies that the tag matches the package version, rebuilds from that tagged
   source, and publishes only through its configured protected environment. A passing
   earlier commit or a Docker-only substitute does not qualify the tag.

These are recurring release conditions, not backlog items or a dated evidence diary.

## Acceptance

Acceptance tests map stable `F*`, `B*`, `J*`, `O*`, and `AC-*` identifiers to observable
user outcomes. Passing lower-level tests does not replace an end-to-end acceptance check
when a change crosses CLI, MCP, browser, persistence, or service boundaries.

## Documentation

Public documentation states current supported behavior, target contracts, and unresolved
evidence gaps separately. Record a command and its healthy result instead of a manual
verification timestamp, commit hash, test count, screenshot pack, or delivery diary.

The core and frozen backlogs contain actions only. Completed work is removed; Git history
retains its disposition.
